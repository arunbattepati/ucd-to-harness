#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UCD → Harness NG YAML Converter (hardened)

What this does
--------------
- Reads one or more UCD export JSON files (via --input/--input-dir).
- Writes Harness YAML under --out, grouped by application (default).
- Creates one Service YAML *per UCD component* and one Pipeline YAML *per UCD application*.
- Enforces Harness validators:
  * Stage names conform to ^[A-Za-z_][-0-9A-Za-z_\\s]{0,127}$
  * Every stage has environment + infrastructureDefinitions + execution
  * Exactly one FetchInstanceScript at the start of each stage
- Service YAML uses serviceDefinition.type: CustomDeployment (no unsupported fields).
- Optional: matches StepGroup templates via a registry file (safe if not present).
- Optionally writes sample StepGroup templates into a single common folder for you to import.

Quick usage
-----------
python Scripts/ucd_to_harness_yaml_converter.py \
  --input-dir ucd_input_files \
  --out harness_out \
  --org default \
  --project ucd2harnessmigration \
  --group-by application

Folder layout (generated)
-------------------------
harness_out/
  .harness/
    common/
      templates/              # optional sample templates (for import in UI)
        stepgroup_java_gradle.yaml
        stepgroup_python_app.yaml
        stepgroup_dotnet.yaml
    <AppA>/
      .harness/
        services/*.yaml
        pipelines/<AppA>_deploy.yaml
    <AppB>/
      .harness/
        services/*.yaml
        pipelines/<AppB>_deploy.yaml

Notes
-----
- Import **templates** first (optional) if you plan to use StepGroup refs.
- Import **services** next, then **pipelines**.
"""

import os, re, sys, json, glob, argparse
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except Exception:
    print("PyYAML is required. Install with: pip install pyyaml")
    raise

# ----------------------------
# Validators & Sanitizers
# ----------------------------
ID_RE  = re.compile(r"^[A-Za-z_][0-9A-Za-z_]{0,127}$")
ID_SAN = re.compile(r"[^0-9A-Za-z_]+")
NM_RE_STRICT = re.compile(r"^[A-Za-z_][-0-9A-Za-z_\s]{0,127}$")

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

def sanitize_name(s: str) -> str:
    """
    Produce a name that matches Harness validator:
      ^[A-Za-z_][-0-9A-Za-z_\\s]{0,127}$
    """
    if not isinstance(s, str):
        s = str(s or "Name")
    # Keep: letters/digits/underscore/hyphen/space
    s = re.sub(r"[^0-9A-Za-z_\-\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        s = "Name"
    if not re.match(r"[A-Za-z_]", s[0]):
        s = "_" + s
    s = s[:128]
    if not NM_RE_STRICT.match(s):
        s = re.sub(r"[^-0-9A-Za-z_\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()[:128]
        if not s or not re.match(r"[A-Za-z_]", s[0]):
            s = "_" + (s or "Name")
        s = s[:128]
    return s

def _walk_fix_ids_and_names(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k == "identifier" and isinstance(v, str) and not ID_RE.match(v):
                obj[k] = sanitize_identifier(v)
            if k == "name" and isinstance(v, str) and not NM_RE_STRICT.match(v):
                obj[k] = sanitize_name(v)
        for v in obj.values():
            _walk_fix_ids_and_names(v)
    elif isinstance(obj, list):
        for i in obj:
            _walk_fix_ids_and_names(i)

# ----------------------------
# UCD helpers
# ----------------------------
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

# ----------------------------
# Template registry (optional)
# ----------------------------
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
        if ta:
            ok = ok and (len(tags_set.intersection(ta)) > 0)
        if ok and tl:
            ok = ok and all(t in tags_set for t in tl)
        if ok and m.get("any_regex"):
            ok = ok and _regex_any(m["any_regex"], hay)
        if ok and m.get("all_regex"):
            ok = ok and _regex_all(m["all_regex"], hay)
        if ok:
            spec = {"name": rule["name"], "templateRef": rule["templateRef"], "versionLabel": rule["versionLabel"]}
            ti = _build_template_inputs(rule.get("inputs") or {})
            if ti:
                spec["templateInputs"] = ti
            matched.append(spec)
            if first_match:
                break
    return matched

# ----------------------------
# Builders
# ----------------------------
def build_service_payload(name: str, identifier: str, tags_map: Dict[str, str]) -> Dict[str, Any]:
    """
    Service YAML:
      serviceDefinition.type: CustomDeployment
      No unsupported fields (e.g., no customDeploymentRef here)
    """
    return {
        "service": {
            "name": sanitize_name(name),
            "identifier": sanitize_identifier(identifier),
            "tags": tags_map or {},
            "serviceDefinition": {
                "type": "CustomDeployment",
                "spec": {
                    "variables": []
                }
            }
        }
    }

def _fetch_instance_step() -> Dict[str, Any]:
    """Exactly once per stage (CustomDeployment)."""
    return {
        "step": {
            "name": "Fetch Instances",
            "identifier": "Fetch_Instances",
            "type": "FetchInstanceScript",
            "timeout": "10m",
            "spec": {
                "shell": "Bash",
                "onDelegate": True,
                "source": {
                    "type": "Inline",
                    "spec": {
                        "script": (
                            'echo "Fetching instances for $HARNESS_SERVICE_NAME..."\n'
                            'echo \'{"instances":[{"name":"sample-instance","id":"1"}]}\'\n'
                        )
                    }
                }
            }
        }
    }

def build_stage_for_component(svc_identifier: str,
                              stage_name: str,
                              matched_stepgroups: List[Dict[str, Any]]) -> Dict[str, Any]:
    disp = sanitize_name(stage_name)
    ident = sanitize_identifier(disp)

    stage = {
        "stage": {
            "name": disp,
            "identifier": ident,
            "type": "Deployment",
            "spec": {
                "deploymentType": "CustomDeployment",
                "service": {"serviceRef": svc_identifier},
                "environment": {
                    "environmentRef": "<+input>",
                    "deployToAll": True,
                    "infrastructureDefinitions": [{"identifier": "input"}]
                },
                "execution": {"steps": []}
            },
            "failureStrategies": [
                {
                    "onFailure": {
                        "errors": ["AllErrors"],
                        "action": {"type": "StageRollback"}
                    }
                }
            ]
        }
    }

    steps = stage["stage"]["spec"]["execution"]["steps"]
    # Ensure first step is FetchInstanceScript
    steps.append(_fetch_instance_step())

    # Optional: template step-groups
    for sg in matched_stepgroups or []:
        sg_name = sanitize_name(sg["name"])
        blk: Dict[str, Any] = {
            "stepGroup": {
                "name": sg_name,
                "identifier": sanitize_identifier(sg_name),
                "template": {
                    "templateRef": sg["templateRef"],
                    "versionLabel": sg.get("versionLabel", "v1")
                }
            }
        }
        if "templateInputs" in sg:
            blk["stepGroup"]["template"]["templateInputs"] = sg["templateInputs"]
        steps.append(blk)

    # Terminal placeholder
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

    # Safety net (ensure required sections):
    spec = stage["stage"].setdefault("spec", {})
    env  = spec.setdefault("environment", {})
    env.setdefault("environmentRef", "<+input>")
    env.setdefault("deployToAll", True)
    infra = env.setdefault("infrastructureDefinitions", [])
    if not isinstance(infra, list) or not infra:
        env["infrastructureDefinitions"] = [{"identifier": "input"}]

    execn = spec.setdefault("execution", {})
    s2 = execn.setdefault("steps", [])
    # Guarantee FetchInstanceScript is first and single
    def _is_fetch(n: Dict[str, Any]) -> bool:
        return isinstance(n, dict) and "step" in n and n["step"].get("type") == "FetchInstanceScript"
    rest = [n for n in s2 if not _is_fetch(n)]
    s2.clear()
    s2.append(_fetch_instance_step())
    s2.extend(rest)

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

# ----------------------------
# YAML writer with meta
# ----------------------------
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
    # validate YAML
    with open(path, "r", encoding="utf-8") as f:
        yaml.safe_load(f)

# ----------------------------
# Common sample templates (optional)
# ----------------------------
def write_common_templates(common_dir: str, org: str, proj: str) -> None:
    """
    Writes sample StepGroup templates you can import in Harness UI (no Git).
    These are optional; pipelines do NOT hard-reference them unless your registry matches.
    """
    tmpl_root = os.path.join(common_dir, "templates")
    os.makedirs(tmpl_root, exist_ok=True)

    def _stepgroup(name: str, ident: str, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "template": {
                "name": sanitize_name(name),
                "identifier": sanitize_identifier(ident),
                "versionLabel": "v1",
                "type": "StepGroup",
                "orgIdentifier": org,
                "projectIdentifier": proj,
                "spec": {
                    "stageType": "Deployment",
                    "steps": steps
                }
            }
        }

    # Java/Gradle
    java_steps = [
        {"step": {"type": "Run", "name": "Gradle Build", "identifier": "Gradle_Build",
                  "spec": {"shell": "Bash", "command": "gradle clean build"}}},
        {"step": {"type": "Run", "name": "Publish Artifact", "identifier": "Publish_Artifact",
                  "spec": {"shell": "Bash", "command": "echo publish artifact placeholder"}}}
    ]
    write_yaml(os.path.join(tmpl_root, "stepgroup_java_gradle.yaml"),
               _stepgroup("Java Gradle Build", "Java_Gradle_Build", java_steps), org, proj)

    # Python
    py_steps = [
        {"step": {"type": "Run", "name": "Python Install", "identifier": "Python_Install",
                  "spec": {"shell": "Bash", "command": "python -m pip install -r requirements.txt"}}},
        {"step": {"type": "Run", "name": "Run App", "identifier": "Run_App",
                  "spec": {"shell": "Bash", "command": "python app.py"}}}
    ]
    write_yaml(os.path.join(tmpl_root, "stepgroup_python_app.yaml"),
               _stepgroup("Python App Deploy", "Python_App_Deploy", py_steps), org, proj)

    # .NET
    dot_steps = [
        {"step": {"type": "Run", "name": "DotNet Restore", "identifier": "DotNet_Restore",
                  "spec": {"shell": "Bash", "command": "dotnet restore"}}},
        {"step": {"type": "Run", "name": "DotNet Publish", "identifier": "DotNet_Publish",
                  "spec": {"shell": "Bash", "command": "dotnet publish -c Release"}}}
    ]
    write_yaml(os.path.join(tmpl_root, "stepgroup_dotnet.yaml"),
               _stepgroup(".NET Build & Publish", "DotNet_Build_Publish", dot_steps), org, proj)

# ----------------------------
# File collection & output grouping
# ----------------------------
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

def base_out_root_for(args, file_path: Optional[str]=None, app_name: Optional[str]=None) -> str:
    if args.group_by == "none":
        return os.path.abspath(args.out)
    if args.group_by == "file":
        if not file_path:
            raise ValueError("group-by=file requires file_path")
        base = os.path.splitext(os.path.basename(file_path))[0]
        return os.path.join(os.path.abspath(args.out), sanitize_identifier(base))
    if args.group_by == "application":
        if not app_name:
            raise ValueError("group-by=application requires app_name")
        return os.path.join(os.path.abspath(args.out), sanitize_identifier(app_name))
    return os.path.abspath(args.out)

def ensure_harness_dirs(base_root: str) -> Tuple[str, str]:
    out_root = os.path.join(base_root, ".harness")
    services_dir = os.path.join(out_root, "services")
    pipelines_dir = os.path.join(out_root, "pipelines")
    os.makedirs(services_dir, exist_ok=True)
    os.makedirs(pipelines_dir, exist_ok=True)
    return services_dir, pipelines_dir

# ----------------------------
# Main
# ----------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Convert UCD export JSON to Harness YAML (compliant & grouped)")
    p.add_argument("--input", action="append", help="Path(s) to UCD JSON. Repeat flag or comma-separated. May include directories.")
    p.add_argument("--input-dir", help="Directory containing UCD JSON files (e.g., ucd_input_files/)")
    p.add_argument("--glob", default="*.json", help="Glob for --input-dir (default: *.json)")
    p.add_argument("--recursive", action="store_true", help="Recurse into subfolders when scanning directories")
    p.add_argument("--out", required=True, help="Output base folder (parents of .harness)")
    p.add_argument("--org", required=True, help="Harness orgIdentifier")
    p.add_argument("--project", required=True, help="Harness projectIdentifier")
    p.add_argument("--registry", default=".harness/templates/template-registry.yaml", help="Path to template registry YAML (optional)")
    p.add_argument("--first-match", action="store_true", help="Stop after first matching template per component")
    p.add_argument("--group-by", choices=["file","application","none"], default="application",
                   help="How to organize outputs under --out (default: application)")
    p.add_argument("--write-common-templates", action="store_true",
                   help="Also write sample StepGroup templates under .harness/common/templates")
    args = p.parse_args()

    files = collect_input_files(args.input or [], args.input_dir, args.glob, args.recursive)
    if not files:
        print("ERROR: No input files found."); sys.exit(2)

    # Load registry (ok if missing)
    registry = load_registry(args.registry)

    # Optional: write sample templates once in common/
    if args.write_common_templates:
        common_dir = os.path.join(os.path.abspath(args.out), ".harness", "common")
        write_common_templates(common_dir, args.org, args.project)
        print(f"[info] Wrote sample StepGroup templates to: {common_dir}/templates")

    grand_apps = grand_svcs = 0

    for idx, path in enumerate(files, 1):
        print(f"\n[{idx}/{len(files)}] Processing: {path}")
        try:
            ucd = load_ucd_json(path)
        except Exception as e:
            print(f"  Skipping (bad JSON): {e}")
            continue

        file_apps = file_svcs = 0
        # file-level dirs if grouping by file
        if args.group_by == "file":
            file_base_root = base_out_root_for(args, file_path=path)
            services_dir, pipelines_dir = ensure_harness_dirs(file_base_root)

        for app in (ucd.get("applications") or []):
            app_meta = app.get("application") or {}
            app_name = app_meta.get("name") or "Application"
            app_tags_flat = collect_tags_flat(app_meta.get("tags") or [])
            app_tags_map  = collect_tags_map(app_meta.get("tags") or [])

            # app-level dirs if grouping by application
            if args.group_by == "application":
                app_base_root = base_out_root_for(args, app_name=app_name)
                services_dir, pipelines_dir = ensure_harness_dirs(app_base_root)
            elif args.group_by == "none":
                services_dir, pipelines_dir = ensure_harness_dirs(base_out_root_for(args))

            stages: List[Dict[str, Any]] = []
            svc_count = 0

            for comp in (app.get("components") or []):
                comp_name = comp.get("name") or "Component"
                comp_tags_flat = collect_tags_flat(comp.get("tags") or [])
                comp_tags_map  = collect_tags_map(comp.get("tags") or {})

                svc_identifier = sanitize_identifier(f"{app_name}_{comp_name}")
                svc_yaml = build_service_payload(comp_name, svc_identifier, {**app_tags_map, **comp_tags_map})
                svc_path = os.path.join(services_dir, f"{svc_identifier}.yaml")
                write_yaml(svc_path, svc_yaml, args.org, args.project)
                svc_count += 1; file_svcs += 1

                matched = match_stepgroups_for_component(app_name, comp_name, app_tags_flat, comp_tags_flat, registry, first_match=args.first_match)

                stage_name = f"Deploy {comp_name}"
                stage = build_stage_for_component(svc_identifier, stage_name, matched)
                stages.append(stage)

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

    print(f"\nAll done. Processed {len(files)} file(s), {grand_apps} applications, {grand_svcs} services.")
    print(f"Output root: {os.path.abspath(args.out)}  (group-by: {args.group_by})")

if __name__ == "__main__":
    main()
