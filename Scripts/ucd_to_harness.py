#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UCD → Harness NG YAML Converter (services, pipelines, and common templates)

What it does
------------
1) Reads UCD export JSONs (via --input and/or --input-dir).
2) Emits per-application Harness YAML:
   <out>/<AppName>/.harness/services/*.yaml
   <out>/<AppName>/.harness/pipelines/*.yaml
3) Generates a _common/ templates pack you can import FIRST in Harness:
   - templates/custom-deployment/Generic_Custom_Deployment.yaml
   - templates/step-groups/*.yaml (Java Gradle, Python, .NET, IIS, Windows Service, MSI)
4) Optionally uses a template-registry (match rules) to attach StepGroups to stages.
5) Ensures identifiers/names are Harness-safe, injects org/project, and validates YAML.

Usage examples
--------------
# Convert a folder of UCD JSONs, grouped per-application
python Scripts/ucd_to_harness_yaml_converter.py \
  --input-dir ucd_input_files \
  --out harness_out \
  --org my_org --project my_project

# Multiple explicit files + a registry for StepGroup matching
python Scripts/ucd_to_harness_yaml_converter.py \
  --input ucd_input_files/a.json --input ucd_input_files/b.json \
  --out harness_out --org default --project ucd2harnessmigration \
  --registry .harness/template-registry.yaml
"""
import os, re, sys, json, glob, argparse
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except Exception:
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    raise

# --------------------------------------------------------------------
# Harness validators (identifiers/names)
# --------------------------------------------------------------------
ID_RE  = re.compile(r"^[A-Za-z_][0-9A-Za-z_]{0,127}$")
NM_RE  = re.compile(r"^[A-Za-z_0-9-.][-0-9A-Za-z_\\s.]{0,127}$")
ID_SAN = re.compile(r"[^0-9A-Za-z_]+")

def sanitize_name(s: str) -> str:
    s = (s or "Name").strip()
    s = s.replace("/", " ").replace("\\", " ").replace("(", " ").replace(")", " ")
    s = re.sub(r"[^0-9A-Za-z_\-\s.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s: s = "Name"
    s = s[:128]
    if not NM_RE.match(s):
        s = s.lstrip()
        if not s or not re.match(r"[A-Za-z_0-9-.]", s[0]):
            s = "_" + s
        s = s[:128]
    if not NM_RE.match(s):
        s = re.sub(r"_+", " ", ID_SAN.sub("_", s))[:128] or "Name"
    return s

def sanitize_identifier(s: str) -> str:
    s = (s or "id").strip()
    s = ID_SAN.sub("_", s)
    if not s or not re.match(r"[A-Za-z_]", s[0]):
        s = "_" + s
    s = re.sub(r"_+", "_", s).strip("_")[:128]
    if not ID_RE.match(s):
        s = "_" + re.sub(r"[^0-9A-Za-z_]", "_", s)
        s = re.sub(r"_+", "_", s).strip("_")[:128]
        if not s:
            s = "id"
    return s

def _walk_fix_ids_and_names(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k == "identifier" and isinstance(v, str) and not ID_RE.match(v):
                obj[k] = sanitize_identifier(v)
            if k == "name" and isinstance(v, str) and not NM_RE.match(v):
                obj[k] = sanitize_name(v)
        for v in obj.values():
            _walk_fix_ids_and_names(v)
    elif isinstance(obj, list):
        for i in obj:
            _walk_fix_ids_and_names(i)

# --------------------------------------------------------------------
# Deployment type inference
# --------------------------------------------------------------------
VALID_DEPLOYMENT_TYPES = {
    "CustomDeployment", "WinRm", "TAS", "Kubernetes", "SSH",
    "NativeHelm", "ECS", "AzureWebApp", "ServerlessAwsLambda", "GoogleCloudRun"
}
SYNONYMS = {
    "pcf": "TAS", "tanzu": "TAS", "cloud foundry": "TAS", "tas": "TAS",
    "windows": "WinRm", "winrm": "WinRm", "iis": "WinRm", "msi_deploy": "WinRm", "windows service": "WinRm",
    "k8s": "Kubernetes", "kubernetes": "Kubernetes"
}

def infer_deployment_type(app_tags: List[str], comp_tags: List[str]) -> str:
    hay = " ".join(app_tags + comp_tags).lower()
    for key, val in SYNONYMS.items():
        if key in hay:
            return val
    return "CustomDeployment"

def normalize_deployment_type(dt: Optional[str]) -> str:
    if not dt:
        return "CustomDeployment"
    if dt not in VALID_DEPLOYMENT_TYPES:
        low = dt.lower()
        for k, v in SYNONYMS.items():
            if k == low:
                return v
        return "CustomDeployment"
    return dt

# --------------------------------------------------------------------
# YAML write helpers
# --------------------------------------------------------------------
def ensure_meta(payload: Dict[str, Any], kind: str, org: str, proj: str) -> None:
    node = payload.get(kind)
    if not isinstance(node, dict):
        return
    node.setdefault("orgIdentifier", org)
    node.setdefault("projectIdentifier", proj)
    if "name" in node and isinstance(node["name"], str):
        node["name"] = sanitize_name(node["name"])
    if "identifier" in node:
        if not isinstance(node["identifier"], str) or not ID_RE.match(node["identifier"]):
            node["identifier"] = sanitize_identifier(str(node["identifier"]))
    elif "name" in node:
        node["identifier"] = sanitize_identifier(str(node["name"]))

def write_yaml(path: str, payload: Dict[str, Any], org: str, proj: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for top in ("pipeline", "service", "template", "environment", "infrastructureDefinition"):
        if top in payload:
            ensure_meta(payload, top, org, proj)
    _walk_fix_ids_and_names(payload)
    text = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    # Validate
    with open(path, "r", encoding="utf-8") as f:
        yaml.safe_load(f)

# --------------------------------------------------------------------
# UCD helpers
# --------------------------------------------------------------------
def load_ucd_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _parse_tag(name: str) -> Tuple[str, str]:
    name = (name or "").strip()
    if ":" in name:
        k, v = name.split(":", 1)
        return k.strip(), v.strip()
    return name, "true"

def collect_tags_map(tag_objs: List[Dict[str, Any]]) -> Dict[str, str]:
    tags: Dict[str, str] = {}
    for t in tag_objs or []:
        raw = t.get("name") if isinstance(t, dict) else str(t)
        k, v = _parse_tag(str(raw))
        if k:
            tags[k] = v
    return tags

def collect_tags_flat(tag_objs: List[Dict[str, Any]]) -> List[str]:
    flat: List[str] = []
    for t in tag_objs or []:
        raw = t.get("name") if isinstance(t, dict) else str(t)
        flat.append(str(raw))
    return flat

# --------------------------------------------------------------------
# Template registry matching (StepGroups)
# --------------------------------------------------------------------
def load_registry(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    items = data.get("templates") or []
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        m = it.get("match") or {}
        out.append({
            "name": it.get("name") or it.get("templateRef") or "StepGroup",
            "templateRef": it.get("templateRef"),
            "versionLabel": it.get("versionLabel", "v1"),
            "match": {
                "tags_any": m.get("tags_any") or [],
                "tags_all": m.get("tags_all") or [],
                "any_regex": m.get("any_regex") or [],
                "all_regex": m.get("all_regex") or [],
            },
            "inputs": it.get("inputs") or {},
        })
    return out

def _regex_any(pats: List[str], hay: str) -> bool:
    for p in pats:
        try:
            if re.search(p, hay, re.IGNORECASE):
                return True
        except re.error:
            pass
    return False

def _regex_all(pats: List[str], hay: str) -> bool:
    for p in pats:
        try:
            if not re.search(p, hay, re.IGNORECASE):
                return False
        except re.error:
            return False
    return True

def _build_template_inputs(inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not inputs:
        return None
    vmap = inputs.get("variables") or {}
    if not vmap:
        return None
    return {"variables": [{"name": str(k), "type": "String", "value": str(v)} for k, v in vmap.items()]}

def match_stepgroups_for_component(app_name: str, comp_name: str,
                                   app_tags: List[str], comp_tags: List[str],
                                   registry: List[Dict[str, Any]],
                                   first_match: bool=False) -> List[Dict[str, Any]]:
    tags_set = set([t.lower() for t in app_tags + comp_tags])
    hay = " ".join([app_name, comp_name] + app_tags + comp_tags)
    matched: List[Dict[str, Any]] = []
    for rule in registry:
        m = rule.get("match", {})
        ok = True
        ta = [t.lower() for t in m.get("tags_any", [])]
        tl = [t.lower() for t in m.get("tags_all", [])]
        if ta: ok = ok and (len(tags_set.intersection(ta)) > 0)
        if ok and tl: ok = ok and all(t in tags_set for t in tl)
        if ok and m.get("any_regex"): ok = ok and _regex_any(m["any_regex"], hay)
        if ok and m.get("all_regex"): ok = ok and _regex_all(m["all_regex"], hay)
        if ok:
            spec = {"name": rule["name"], "templateRef": rule["templateRef"], "versionLabel": rule["versionLabel"]}
            ti = _build_template_inputs(rule.get("inputs") or {})
            if ti:
                spec["templateInputs"] = ti
            matched.append(spec)
            if first_match:
                break
    return matched

# --------------------------------------------------------------------
# COMMON TEMPLATES (CustomDeployment + StepGroups)
# --------------------------------------------------------------------
def _write_yaml(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def _sg_shell_step(name: str, identifier: str, script: str) -> dict:
    return {
        "step": {
            "type": "ShellScript",
            "name": name,
            "identifier": identifier,
            "spec": {
                "shell": "Bash",
                "onDelegate": True,
                "source": {"type": "Inline", "spec": {"script": script}}
            }
        }
    }

def _stepgroup_template(name: str, ident: str, org: str, project: str, steps: list) -> dict:
    return {
        "template": {
            "name": name,
            "identifier": ident,
            "versionLabel": "v1",
            "type": "StepGroup",
            "orgIdentifier": org,
            "projectIdentifier": project,
            "spec": {
                "stageType": "Deployment",
                "steps": steps
            }
        }
    }

def emit_custom_deployment_template(out_root: str, org: str, project: str) -> str:
    """
    Emits a minimal Custom Deployment template.
    Services set spec.customDeploymentRef to this identifier.
    """
    ident = "Generic_Custom_Deployment"
    tmpl = {
        "template": {
            "name": "Generic Custom Deployment",
            "identifier": ident,
            "versionLabel": "v1",
            "type": "CustomDeployment",
            "orgIdentifier": org,
            "projectIdentifier": project,
            "spec": {
                "infrastructure": {
                    "fetchInstancesScript": (
                        'echo \'{"instances":[{"name":"sample-instance","id":"1"}]}\'\n'
                    ),
                    "instancesListPath": "instances",
                    "instanceAttributes": ["name", "id"]
                }
            }
        }
    }
    path = os.path.join(out_root, "_common", "templates", "custom-deployment", f"{ident}.yaml")
    _write_yaml(path, tmpl)
    return ident  # return templateRef to use in services

def emit_stepgroup_templates(out_root: str, org: str, project: str) -> List[str]:
    base = os.path.join(out_root, "_common", "templates", "step-groups")
    os.makedirs(base, exist_ok=True)
    emitted: List[str] = []

    # Java / Gradle
    java_gradle = _stepgroup_template(
        "Java Gradle Build", "Java_Gradle_Build", org, project,
        [
            _sg_shell_step("Gradle Build", "Gradle_Build", """\
set -e
echo "Running Gradle build..."
if command -v ./gradlew >/dev/null 2>&1; then
  ./gradlew clean build
else
  gradle clean build
fi
"""),
            _sg_shell_step("Publish Artifact (placeholder)", "Publish_Artifact", """\
echo "Publish artifact step goes here (Artifactory/S3)."
""")
        ]
    )
    _write_yaml(os.path.join(base, "Java_Gradle_Build.yaml"), java_gradle); emitted.append("Java_Gradle_Build")

    # Python App
    python_app = _stepgroup_template(
        "Python App Deploy", "Python_App_Deploy", org, project,
        [
            _sg_shell_step("Create venv & Install", "Python_Venv_Install", """\
set -e
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
"""),
            _sg_shell_step("Run App (placeholder)", "Python_Run", """\
echo "Start your Flask/Django process here (gunicorn/uvicorn/etc)."
""")
        ]
    )
    _write_yaml(os.path.join(base, "Python_App_Deploy.yaml"), python_app); emitted.append("Python_App_Deploy")

    # .NET
    dotnet = _stepgroup_template(
        ".NET Build & Publish", "DotNet_Build_Publish", org, project,
        [
            _sg_shell_step(".NET Restore/Build", "DotNet_Restore_Build", """\
set -e
dotnet --info
dotnet restore
dotnet build --configuration Release
"""),
            _sg_shell_step(".NET Publish", "DotNet_Publish", """\
set -e
dotnet publish --configuration Release -o out/
""")
        ]
    )
    _write_yaml(os.path.join(base, "DotNet_Build_Publish.yaml"), dotnet); emitted.append("DotNet_Build_Publish")

    # IIS Web Deploy
    iis_webdeploy = _stepgroup_template(
        "IIS MS Web Deploy", "IIS_MS_Web_Deploy", org, project,
        [
            _sg_shell_step("IIS WebDeploy (placeholder)", "IIS_WebDeploy", """\
echo "Use PowerShell + WebDeploy here to publish the package to IIS."
""")
        ]
    )
    _write_yaml(os.path.join(base, "IIS_MS_Web_Deploy.yaml"), iis_webdeploy); emitted.append("IIS_MS_Web_Deploy")

    # Windows Service Control
    win_service = _stepgroup_template(
        "Windows Service Control", "Windows_Service", org, project,
        [
            _sg_shell_step("Stop Service", "Stop_Service", """\
echo "Use PowerShell WinRM to Stop-Service <Name>"
"""),
            _sg_shell_step("Start Service", "Start_Service", """\
echo "Use PowerShell WinRM to Start-Service <Name>"
""")
        ]
    )
    _write_yaml(os.path.join(base, "Windows_Service.yaml"), win_service); emitted.append("Windows_Service")

    # MSI Deploy
    msi = _stepgroup_template(
        "Windows MSI Deploy", "MSI_Deploy", org, project,
        [
            _sg_shell_step("Install MSI", "MSI_Install", """\
echo "Use msiexec.exe /i <msi> /qn or similar via WinRM."
""")
        ]
    )
    _write_yaml(os.path.join(base, "MSI_Deploy.yaml"), msi); emitted.append("MSI_Deploy")

    return emitted

def ensure_common_templates(out_root: str, org: str, project: str) -> str:
    os.makedirs(os.path.join(out_root, "_common", "templates"), exist_ok=True)
    cdt_ref = emit_custom_deployment_template(out_root, org, project)
    emit_stepgroup_templates(out_root, org, project)
    # README for templates
    readme = os.path.join(out_root, "_common", "README.md")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(
            "# Common Templates (import first)\n\n"
            "Import these into Harness **before** services/pipelines:\n\n"
            "1. Template → New Template → Upload YAML → `templates/custom-deployment/Generic_Custom_Deployment.yaml`\n"
            "2. Template → New Template → Upload YAML → all files in `templates/step-groups/` (Project scope)\n"
        )
    return cdt_ref

# --------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------
def build_service_payload(name: str, identifier: str, tags_map: Dict[str, str],
                          custom_deployment_template_ref: str) -> Dict[str, Any]:
    """
    ServiceDefinition uses CustomDeployment and points to a local Custom Deployment Template
    so import succeeds without manual selection.
    """
    return {
        "service": {
            "name": sanitize_name(name),
            "identifier": sanitize_identifier(identifier),
            "tags": tags_map or {},
            "serviceDefinition": {
                "type": "CustomDeployment",
                "spec": {
                    # IMPORTANT: a concrete reference, not <+input>, to avoid schema errors
                    "customDeploymentRef": custom_deployment_template_ref,
                    "variables": []
                }
            }
        }
    }

def build_stage_for_component(svc_identifier: str,
                              stage_name: str,
                              deployment_type: str,
                              matched_stepgroups: List[Dict[str, Any]]) -> Dict[str, Any]:
    dt = normalize_deployment_type(deployment_type)
    stage = {
        "stage": {
            "name": sanitize_name(stage_name),
            "identifier": sanitize_identifier(stage_name),
            "type": "Deployment",
            "spec": {
                "deploymentType": dt,
                "service": {"serviceRef": svc_identifier},
                "environment": {
                    "environmentRef": "<+input>",
                    "deployToAll": True,
                    "infrastructureDefinitions": [{"identifier": "<+input>"}],
                },
                "execution": {"steps": []},
            },
            "failureStrategies": [
                {"onFailure": {"errors": ["AllErrors"], "action": {"type": "StageRollback"}}}
            ]
        }
    }

    steps = stage["stage"]["spec"]["execution"]["steps"]

    # Attach matched StepGroups (by templateRef)
    for sg in matched_stepgroups:
        stepgroup_block = {
            "stepGroup": {
                "name": sanitize_name(sg["name"]),
                "identifier": sanitize_identifier(sg["name"]),
                "template": {
                    "templateRef": sg["templateRef"],
                    "versionLabel": sg.get("versionLabel", "v1")
                }
            }
        }
        if "templateInputs" in sg:
            stepgroup_block["stepGroup"]["template"]["templateInputs"] = sg["templateInputs"]
        steps.append(stepgroup_block)

    # Terminal placeholder to make the stage valid even if no StepGroups matched
    steps.append({
        "step": {
            "name": "Deploy",
            "identifier": "Deploy",
            "type": "ShellScript",
            "spec": {
                "shell": "Bash",
                "onDelegate": True,
                "source": {"type": "Inline", "spec": {"script": "echo TODO: implement deployment"}}
            }
        }
    })
    return stage

def build_pipeline_payload(pipeline_name: str,
                           pipeline_id: str,
                           org: str, proj: str,
                           stages: List[Dict[str, Any]],
                           pipeline_tags: Optional[Dict[str, str]]=None) -> Dict[str, Any]:
    return {
        "pipeline": {
            "name": sanitize_name(pipeline_name),
            "identifier": sanitize_identifier(pipeline_id),
            "orgIdentifier": org,
            "projectIdentifier": proj,
            "tags": pipeline_tags or {},
            "stages": stages
        }
    }

# --------------------------------------------------------------------
# File collection & output grouping
# --------------------------------------------------------------------
def collect_input_files(inputs: List[str], input_dir: Optional[str], glob_pattern: str, recursive: bool) -> List[str]:
    files: List[str] = []
    for item in inputs or []:
        for part in [p.strip() for p in item.split(",") if p.strip()]:
            if os.path.isdir(part):
                pattern = os.path.join(part, "**", glob_pattern) if recursive else os.path.join(part, glob_pattern)
                files.extend(glob.glob(pattern, recursive=recursive))
            else:
                files.append(part)
    if input_dir:
        pattern = os.path.join(input_dir, "**", glob_pattern) if recursive else os.path.join(input_dir, glob_pattern)
        files.extend(glob.glob(pattern, recursive=recursive))
    normed, seen = [], set()
    for f in files:
        p = os.path.abspath(f)
        if os.path.isfile(p) and p.endswith(".json") and p not in seen:
            seen.add(p); normed.append(p)
    return normed

def app_out_root(base_out: str, app_name: str) -> str:
    return os.path.join(os.path.abspath(base_out), sanitize_identifier(app_name))

def ensure_harness_dirs(base_root: str) -> Tuple[str, str]:
    out_root = os.path.join(base_root, ".harness")
    services_dir = os.path.join(out_root, "services")
    pipelines_dir = os.path.join(out_root, "pipelines")
    os.makedirs(services_dir, exist_ok=True)
    os.makedirs(pipelines_dir, exist_ok=True)
    return services_dir, pipelines_dir

# --------------------------------------------------------------------
# README writer for the whole run
# --------------------------------------------------------------------
def write_out_readme(out_root: str) -> None:
    text = f"""# Harness Output (from converter)

This folder contains:
- `_common/templates/custom-deployment/Generic_Custom_Deployment.yaml` – import **first**.
- `_common/templates/step-groups/*.yaml` – import all StepGroup templates (Project scope).
- `<AppName>/.harness/services/*.yaml` – one Service per UCD component (already wired to the Custom Deployment template).
- `<AppName>/.harness/pipelines/*.yaml` – one Pipeline per UCD application (stages include matched StepGroups).

## Import order in Harness (no Git)
1. **Templates → New Template → Upload YAML**: `_common/templates/custom-deployment/Generic_Custom_Deployment.yaml`
2. **Templates → New Template → Upload YAML**: all YAMLs in `_common/templates/step-groups/`
3. **Services → New Service → Import from YAML**: import each `<AppName>/.harness/services/*.yaml`
4. **Pipelines → Create → Import from YAML**: import each `<AppName>/.harness/pipelines/*.yaml`

> Pipelines prompt for `environmentRef` and `infrastructureDefinitions[0].identifier` at run time.
"""
    with open(os.path.join(out_root, "README.md"), "w", encoding="utf-8") as f:
        f.write(text)

# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Convert UCD export JSON to Harness YAML (services, pipelines, and common templates)")
    p.add_argument("--input", action="append", help="Path(s) to UCD JSON. Repeat flag or comma-separated. May include directories.")
    p.add_argument("--input-dir", help="Directory containing UCD JSON files (e.g., ucd_input_files/)")
    p.add_argument("--glob", default="*.json", help="Glob for --input-dir (default: *.json)")
    p.add_argument("--recursive", action="store_true", help="Recurse into subfolders when scanning directories")
    p.add_argument("--out", required=True, help="Output base folder")
    p.add_argument("--org", required=True, help="Harness orgIdentifier")
    p.add_argument("--project", required=True, help="Harness projectIdentifier")
    p.add_argument("--registry", default=".harness/template-registry.yaml", help="Path to template registry YAML (optional)")
    p.add_argument("--first-match", action="store_true", help="Stop after first matching StepGroup per component")
    args = p.parse_args()

    files = collect_input_files(args.input or [], args.input_dir, args.glob, args.recursive)
    if not files:
        print("ERROR: No input files found.", file=sys.stderr)
        sys.exit(2)

    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)

    # 1) Emit common templates pack first
    custom_deployment_ref = ensure_common_templates(out_root, args.org, args.project)

    # 2) Load registry for StepGroup matching (optional)
    registry = load_registry(args.registry)

    # 3) Convert each file/application
    grand_apps = grand_svcs = 0
    for idx, path in enumerate(files, 1):
        print(f"\n[{idx}/{len(files)}] Processing: {path}")
        try:
            ucd = load_ucd_json(path)
        except Exception as e:
            print(f"  Skipping (bad JSON): {e}")
            continue

        file_apps = file_svcs = 0
        for app in (ucd.get("applications") or []):
            app_meta = app.get("application") or {}
            app_name = app_meta.get("name") or "Application"
            app_tags_flat = collect_tags_flat(app_meta.get("tags") or [])
            app_tags_map  = collect_tags_map(app_meta.get("tags") or [])

            app_root = app_out_root(out_root, app_name)
            services_dir, pipelines_dir = ensure_harness_dirs(app_root)

            stages: List[Dict[str, Any]] = []
            svc_count = 0

            for comp in (app.get("components") or []):
                comp_name = comp.get("name") or "Component"
                comp_tags_flat = collect_tags_flat(comp.get("tags") or [])
                comp_tags_map  = collect_tags_map(comp.get("tags") or [])

                svc_identifier = sanitize_identifier(f"{app_name}_{comp_name}")

                # Service (CustomDeployment wired to our common template)
                svc_yaml = build_service_payload(
                    name=comp_name,
                    identifier=svc_identifier,
                    tags_map={**app_tags_map, **comp_tags_map},
                    custom_deployment_template_ref=custom_deployment_ref
                )
                svc_path = os.path.join(services_dir, f"{svc_identifier}.yaml")
                write_yaml(svc_path, svc_yaml, args.org, args.project)
                svc_count += 1; file_svcs += 1

                # Stage with optional StepGroups
                matched = match_stepgroups_for_component(app_name, comp_name, app_tags_flat, comp_tags_flat, registry, first_match=args.first_match)
                deployment_type = infer_deployment_type(app_tags_flat, comp_tags_flat)
                stage_name = f"Deploy {comp_name}"
                stage = build_stage_for_component(svc_identifier, stage_name, deployment_type, matched)
                stages.append(stage)

            # Pipeline for the application
            pipeline_name = f"{app_name} deploy"
            pipeline_id   = f"{app_name}_deploy"
            pipeline_yaml = build_pipeline_payload(pipeline_name, pipeline_id, args.org, args.project, stages, app_tags_map)
            pipe_path = os.path.join(pipelines_dir, f"{sanitize_identifier(pipeline_id)}.yaml")
            write_yaml(pipe_path, pipeline_yaml, args.org, args.project)

            file_apps += 1
            print(f"  Converted application: {app_name}  ->  {svc_count} services, 1 pipeline")

        grand_apps += file_apps
        grand_svcs += file_svcs
        print(f"  File summary: {file_apps} applications, {file_svcs} services")

    # 4) Write a top-level README with import order & structure
    write_out_readme(out_root)

    print(f"\nAll done. Processed {len(files)} file(s), {grand_apps} applications, {grand_svcs} services.")
    print(f"Output root: {out_root}")

if __name__ == "__main__":
    main()
