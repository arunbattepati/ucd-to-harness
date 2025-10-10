#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UCD → Harness NG converter (multi-file, clean YAML, reusable templates)

- Zero-touch for CustomDeployment: services always reference a bundled
  Custom Deployment Template ("Generic_Custom_Deployment") that this
  script writes under .harness/templates/custom/.
- Python 3.9+ compatible.
- Accepts multiple --input files (repeatable or comma-separated) and/or --input-dir.
- Recursively scans a directory if --recursive is used (defaults to *.json).
- Produces Harness-compliant YAML (indentation, booleans, structure).
- Sanitizes all identifiers to [A-Za-z0-9_]{1,128}.
- Ensures globally unique Service identifiers: {AppName}_{ComponentName}.
- Injects orgIdentifier / projectIdentifier into top-level entities.
- Matches reusable StepGroup templates via .harness/template-registry.yaml.
- Re-parses written YAML for quick validation.

Examples
--------
# Sweep a directory of UCD exports and group output per file
python Scripts/ucd_to_harness.py \
  --input-dir ucd_input_files --recursive \
  --out harness_out --org my_org --project my_project \
  --group-by file

# Group by application name
python Scripts/ucd_to_harness.py \
  --input-dir ucd_input_files \
  --out harness_out --org my_org --project my_project \
  --group-by application
"""
import os, re, sys, json, glob, argparse
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except Exception as e:
    print("PyYAML is required: pip install pyyaml")
    raise

# --------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------
DEFAULT_CUSTOM_DEPLOYMENT_IDENTIFIER = "Generic_Custom_Deployment"
DEFAULT_CUSTOM_DEPLOYMENT_NAME = "Generic Custom Deployment"
DEFAULT_CUSTOM_DEPLOYMENT_VERSION = "v1"

# --------------------------------------------------------------------
# Harness validators
# identifier: ^[a-zA-Z_][0-9a-zA-Z_]{0,127}$
# name:       ^[a-zA-Z_0-9-.][-0-9a-zA-Z_\\s.]{0,127}$
# --------------------------------------------------------------------
ID_RE  = re.compile(r"^[A-Za-z_][0-9A-Za-z_]{0,127}$")
NM_RE  = re.compile(r"^[A-Za-z_0-9-.][-0-9A-Za-z_\s.]{0,127}$")
ID_SAN = re.compile(r"[^0-9A-Za-z_]+")

def sanitize_name(s: str) -> str:
    s = (s or "Name").strip()
    s = s.replace("/", " ").replace("\\", " ").replace("(", " ").replace(")", " ")
    s = re.sub(r"[^0-9A-Za-z_\-\s.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        s = "Name"
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
# deploymentType normalization
# --------------------------------------------------------------------
VALID_DEPLOYMENT_TYPES = {
    "CustomDeployment", "WinRm", "TAS", "Kubernetes", "SSH", "NativeHelm", "ECS", "AzureWebApp", "ServerlessAwsLambda", "GoogleCloudRun"
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
# YAML helpers
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
# Template registry (optional StepGroups)
# --------------------------------------------------------------------
def load_registry(path: Optional[str]) -> Dict[str, Any]:
    data = {"templates": [], "customDeployment": {}}
    if not path or not os.path.exists(path):
        return data
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    data["templates"] = raw.get("templates") or []
    # Optional: let users override the default customDeploymentRef via registry
    cd = raw.get("customDeployment") or {}
    # shape: { defaultRef: "Identifier", rules: [{tags_any:[], any_regex:[], ref:"..."}] }
    data["customDeployment"] = {
        "defaultRef": cd.get("defaultRef", DEFAULT_CUSTOM_DEPLOYMENT_IDENTIFIER),
        "rules": cd.get("rules") or []
    }
    return data

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
                                   registry: Dict[str, Any],
                                   first_match: bool=False) -> List[Dict[str, Any]]:
    rules = registry.get("templates") or []
    tags_set = set([t.lower() for t in app_tags + comp_tags])
    hay = " ".join([app_name, comp_name] + app_tags + comp_tags)
    matched: List[Dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        m = (rule.get("match") or {})
        ok = True
        ta = [t.lower() for t in (m.get("tags_any") or [])]
        tl = [t.lower() for t in (m.get("tags_all") or [])]
        if ta: ok = ok and (len(tags_set.intersection(ta)) > 0)
        if ok and tl: ok = ok and all(t in tags_set for t in tl)
        if ok and m.get("any_regex"): ok = ok and _regex_any(m["any_regex"], hay)
        if ok and m.get("all_regex"): ok = ok and _regex_all(m["all_regex"], hay)
        if ok:
            spec = {
                "name": rule.get("name") or rule.get("templateRef") or "StepGroup",
                "templateRef": rule.get("templateRef"),
                "versionLabel": rule.get("versionLabel", "v1")
            }
            ti = _build_template_inputs(rule.get("inputs") or {})
            if ti:
                spec["templateInputs"] = ti
            matched.append(spec)
            if first_match:
                break
    return matched

def resolve_custom_deployment_ref(app_name: str, comp_name: str,
                                  app_tags: List[str], comp_tags: List[str],
                                  registry: Dict[str, Any]) -> str:
    """Pick a Custom Deployment Template identifier with zero user input."""
    meta = registry.get("customDeployment") or {}
    default_ref = meta.get("defaultRef") or DEFAULT_CUSTOM_DEPLOYMENT_IDENTIFIER
    rules = meta.get("rules") or []
    tags_set = set([t.lower() for t in app_tags + comp_tags])
    hay = " ".join([app_name, comp_name] + app_tags + comp_tags)
    for r in rules:
        ta = [t.lower() for t in (r.get("tags_any") or [])]
        if ta and not (tags_set.intersection(ta)):
            continue
        if r.get("any_regex") and not _regex_any(r["any_regex"], hay):
            continue
        if r.get("all_regex") and not _regex_all(r["all_regex"], hay):
            continue;
        ref = r.get("ref")
        if ref:
            return sanitize_identifier(ref)
    return sanitize_identifier(default_ref or DEFAULT_CUSTOM_DEPLOYMENT_IDENTIFIER)

# --------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------
def build_service_payload(name: str, identifier: str, tags_map: Dict[str, str], custom_deploy_ref: str) -> Dict[str, Any]:
    # CustomDeployment with a concrete template ref (no runtime input).
    return {
        "service": {
            "name": sanitize_name(name),
            "identifier": sanitize_identifier(identifier),
            "tags": tags_map or {},
            "serviceDefinition": {
                "type": "CustomDeployment",
                "spec": {
                    "customDeploymentRef": sanitize_identifier(custom_deploy_ref),
                    "variables": []
                }
            }
        }
    }

def _fetch_instance_step() -> Dict[str, Any]:
    # One instance discovery step per CustomDeployment stage.
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
                            'echo "Discovering instances for $HARNESS_SERVICE_NAME..."\n'
                            'echo \'{"instances":[{"name":"sample-instance","id":"1"}]}\'\n'
                        )
                    }
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

    if dt == "CustomDeployment":
        steps.append(_fetch_instance_step())

    for sg in matched_stepgroups:
        block = {
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
            block["stepGroup"]["template"]["templateInputs"] = sg["templateInputs"]
        steps.append(block)

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
# Custom Deployment Template emission (one-time, per output root)
# --------------------------------------------------------------------
def write_generic_custom_deployment_template(base_root: str, org: str, proj: str,
                                             identifier: str = DEFAULT_CUSTOM_DEPLOYMENT_IDENTIFIER,
                                             name: str = DEFAULT_CUSTOM_DEPLOYMENT_NAME,
                                             version: str = DEFAULT_CUSTOM_DEPLOYMENT_VERSION) -> None:
    """Emit a minimal, valid Custom Deployment Template so services import cleanly."""
    tmpl_dir = os.path.join(base_root, ".harness", "templates", "custom")
    os.makedirs(tmpl_dir, exist_ok=True)
    path = os.path.join(tmpl_dir, f"{sanitize_identifier(identifier)}.yaml")

    # Only (re)write if missing to avoid clobbering user-edited templates
    if os.path.exists(path):
        return

    payload = {
        "template": {
            "name": sanitize_name(name),
            "identifier": sanitize_identifier(identifier),
            "orgIdentifier": org,
            "projectIdentifier": proj,
            "versionLabel": version,
            "type": "CustomDeployment",
            "spec": {
                # Minimal viable spec: empty variables + a fetch script + a basic attribute
                "variables": [],
                "fetchInstanceScript": {
                    "type": "ShellScript",
                    "timeout": "10m",
                    "spec": {
                        "shell": "Bash",
                        "onDelegate": True,
                        "source": {
                            "type": "Inline",
                            "spec": {
                                "script": (
                                    'echo "Template-level discovery for $HARNESS_SERVICE_NAME"\n'
                                    'echo \'{"instances":[{"name":"template-instance","id":"t1"}]}\'\n'
                                )
                            }
                        }
                    }
                },
                "instanceAttributes": ["name", "id"]
            }
        }
    }
    write_yaml(path, payload, org, proj)

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

# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Convert UCD export JSON to Harness YAML (compliant & grouped)")
    p.add_argument("--input", action="append", help="Path(s) to UCD JSON. Repeat flag or comma-separated. May include directories.")
    p.add_argument("--input-dir", help="Directory containing UCD JSON files (e.g., ucd_input_files/)")
    p.add_argument("--glob", default="*.json", help="Glob for --input-dir (default: *.json)")
    p.add_argument("--recursive", action="store_true", help="Recurse into subfolders when scanning directories")
    p.add_argument("--out", required=True, help="Output base folder (parents of .harness)")
    p.add_argument("--org", required=True, help="Harness orgIdentifier")
    p.add_argument("--project", required=True, help="Harness projectIdentifier")
    p.add_argument("--registry", default=".harness/template-registry.yaml", help="Path to template registry YAML (optional)")
    p.add_argument("--first-match", action="store_true", help="Stop after first matching template per component")
    p.add_argument("--group-by", choices=["file","application","none"], default="file",
                   help="How to organize outputs under --out (default: file)")
    args = p.parse_args()

    files = collect_input_files(args.input or [], args.input_dir, args.glob, args.recursive)
    if not files:
        print("ERROR: No input files found."); sys.exit(2)

    registry = load_registry(args.registry)
    grand_apps = grand_svcs = 0

    # For each grouping root, ensure the custom-deployment template exists once.
    grouping_roots_seen: set[str] = set()

    for idx, path in enumerate(files, 1):
        print(f"\n[{idx}/{len(files)}] Processing: {path}")
        try:
            ucd = load_ucd_json(path)
        except Exception as e:
            print(f"  Skipping (bad JSON): {e}")
            continue

        file_apps = file_svcs = 0
        if args.group_by == "file":
            file_base_root = base_out_root_for(args, file_path=path)
            services_dir, pipelines_dir = ensure_harness_dirs(file_base_root)
            if file_base_root not in grouping_roots_seen:
                write_generic_custom_deployment_template(file_base_root, args.org, args.project)
                grouping_roots_seen.add(file_base_root)

        for app in (ucd.get("applications") or []):
            app_meta = app.get("application") or {}
            app_name = app_meta.get("name") or "Application"
            app_tags_flat = collect_tags_flat(app_meta.get("tags") or [])
            app_tags_map  = collect_tags_map(app_meta.get("tags") or [])

            if args.group_by == "application":
                app_base_root = base_out_root_for(args, app_name=app_name)
                services_dir, pipelines_dir = ensure_harness_dirs(app_base_root)
                if app_base_root not in grouping_roots_seen:
                    write_generic_custom_deployment_template(app_base_root, args.org, args.project)
                    grouping_roots_seen.add(app_base_root)
            elif args.group_by == "none":
                base_root = base_out_root_for(args)
                services_dir, pipelines_dir = ensure_harness_dirs(base_root)
                if base_root not in grouping_roots_seen:
                    write_generic_custom_deployment_template(base_root, args.org, args.project)
                    grouping_roots_seen.add(base_root)

            stages: List[Dict[str, Any]] = []
            svc_count = 0

            for comp in (app.get("components") or []):
                comp_name = comp.get("name") or "Component"
                comp_tags_flat = collect_tags_flat(comp.get("tags") or [])
                comp_tags_map  = collect_tags_map(comp.get("tags") or [])

                svc_identifier = sanitize_identifier(f"{app_name}_{comp_name}")

                # Resolve a concrete customDeploymentRef automatically (uses registry rules if any).
                custom_ref = resolve_custom_deployment_ref(app_name, comp_name, app_tags_flat, comp_tags_flat, registry)

                svc_yaml = build_service_payload(comp_name, svc_identifier, {**app_tags_map, **comp_tags_map}, custom_ref)
                svc_path = os.path.join(services_dir, f"{svc_identifier}.yaml")
                write_yaml(svc_path, svc_yaml, args.org, args.project)
                svc_count += 1; file_svcs += 1

                matched = match_stepgroups_for_component(app_name, comp_name, app_tags_flat, comp_tags_flat, registry, first_match=args.first_match)
                deployment_type = infer_deployment_type(app_tags_flat, comp_tags_flat)

                stage_name = f"Deploy {comp_name}"
                stage = build_stage_for_component(svc_identifier, stage_name, deployment_type, matched)
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
