#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UCD → Harness NG YAML Converter (services + pipelines + common templates)

- Python 3.9+ compatible.
- Accepts multiple --input files and/or --input-dir (with optional --recursive).
- Groups output per application:  <out>/<ApplicationName>/.harness/{services,pipelines}
- Generates:
  * Service YAML (CustomDeployment, with customDeploymentRef → Generic_Custom_Deployment)
  * Pipeline YAML (Deployment stages are CustomDeployment and always include 1 FetchInstanceScript step at top)
  * Common templates under: <out>/common/.harness/templates/
      - Custom Deployment template: Generic_Custom_Deployment.yaml
      - Example StepGroup templates you can customize

USAGE EXAMPLES
--------------
# Convert a directory of UCD exports
python Scripts/ucd_to_harness_yaml_converter.py \
  --input-dir ucd_input_files --recursive \
  --out harness_out --org default --project ucd2harnessmigration

# Convert explicit file(s)
python Scripts/ucd_to_harness_yaml_converter.py \
  --input ucd_input_files/ucd-0822.json,ucd_input_files/ucd-demo.json \
  --out harness_out --org default --project ucd2harnessmigration
"""
import os, re, sys, json, glob, argparse
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except Exception:
    print("PyYAML is required. Install with:  pip install pyyaml")
    raise

# ----------------------------- Validators -----------------------------
ID_RE  = re.compile(r"^[A-Za-z_][0-9A-Za-z_]{0,127}$")
NM_RE  = re.compile(r"^[A-Za-z_0-9-.][-0-9A-Za-z_\\s.]{0,127}$")
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

# ----------------------------- YAML helpers -----------------------------
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
        yaml.safe_load(f)  # quick validation

# ----------------------------- UCD helpers -----------------------------
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

# ------------------------ Template registry (optional) ------------------------
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

# ----------------------------- Builders -----------------------------
def build_service_payload(name: str, identifier: str, tags_map: Dict[str, str],
                          custom_deploy_ref: str) -> Dict[str, Any]:
    # IMPORTANT: Keep 'CustomDeployment' and provide customDeploymentRef
    return {
        "service": {
            "name": sanitize_name(name),
            "identifier": sanitize_identifier(identifier),
            "tags": tags_map or {},
            "serviceDefinition": {
                "type": "CustomDeployment",
                "spec": {
                    "customDeploymentRef": custom_deploy_ref,
                    "variables": []
                }
            }
        }
    }

def _fetch_instance_step() -> Dict[str, Any]:
    """Minimal FetchInstanceScript (required exactly once in CustomDeployment stages)."""
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
                        # Output a minimal discovery JSON; replace with real discovery as needed
                        "script": (
                            'echo "Discovering instances for $HARNESS_SERVICE_NAME"\n'
                            'echo \'{"instances":[{"name":"sample-instance","id":"1"}]}\'\n'
                        )
                    }
                }
            }
        }
    }

def _count_fetch_steps(steps: List[Dict[str, Any]]) -> int:
    c = 0
    for s in steps or []:
        if "step" in s and isinstance(s["step"], dict) and s["step"].get("type") == "FetchInstanceScript":
            c += 1
    return c

def _dedupe_and_prepend_fetch(steps: List[Dict[str, Any]]) -> None:
    """Ensure exactly one FetchInstanceScript, placed as FIRST step."""
    new_steps: List[Dict[str, Any]] = []
    for s in steps:
        if not ("step" in s and isinstance(s["step"], dict) and s["step"].get("type") == "FetchInstanceScript"):
            new_steps.append(s)
    new_steps.insert(0, _fetch_instance_step())
    steps.clear()
    steps.extend(new_steps)

def build_stage_for_component(svc_identifier: str,
                              stage_name: str,
                              matched_stepgroups: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Keep stage deployment type aligned with Service (CustomDeployment)
    dt = "CustomDeployment"
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

    # 1) Guarantee exactly one FetchInstanceScript at top
    _dedupe_and_prepend_fetch(steps)

    # 2) Add matched StepGroups (if any)
    for sg in matched_stepgroups:
        block: Dict[str, Any] = {
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

    # 3) End with a no-op placeholder
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

    # Final guard
    if _count_fetch_steps(steps) != 1 or not (steps and steps[0].get("step", {}).get("type") == "FetchInstanceScript"):
        _dedupe_and_prepend_fetch(steps)

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

# ------------------------ Common Templates emission ------------------------
def write_common_templates(out_root: str, org: str, proj: str,
                           custom_deploy_identifier: str = "Generic_Custom_Deployment") -> None:
    """
    Emit a minimal Custom Deployment template and example StepGroup templates
    that you can import FIRST in Harness (before services/pipelines).
    """
    tmpl_dir = os.path.join(out_root, "common", ".harness", "templates")
    os.makedirs(tmpl_dir, exist_ok=True)

    # 1) Custom Deployment Template (minimal)
    cdt = {
        "template": {
            "name": "Generic Custom Deployment",
            "identifier": custom_deploy_identifier,
            "versionLabel": "v1",
            "type": "CustomDeployment",
            "orgIdentifier": org,
            "projectIdentifier": proj,
            "spec": {
                # Minimal infra model; Harness will use this with FetchInstanceScript step in stage
                "infrastructure": {
                    "variables": []
                },
                "variables": []
            }
        }
    }
    write_yaml(os.path.join(tmpl_dir, f"{custom_deploy_identifier}.yaml"), cdt, org, proj)

    # 2) Example StepGroup templates (stageType required by Harness)
    stepgroups = [
        {
            "file": "Java_Gradle_Build.yaml",
            "payload": {
                "template": {
                    "name": "Java Gradle Build",
                    "identifier": "Java_Gradle_Build",
                    "versionLabel": "v1",
                    "type": "StepGroup",
                    "orgIdentifier": org,
                    "projectIdentifier": proj,
                    "spec": {
                        "stageType": "Deployment",
                        "steps": [
                            {
                                "step": {
                                    "type": "Run",
                                    "name": "Gradle Build",
                                    "identifier": "Gradle_Build",
                                    "spec": {
                                        "shell": "Bash",
                                        "command": "gradle clean build"
                                    }
                                }
                            },
                            {
                                "step": {
                                    "type": "Run",
                                    "name": "Publish Artifact",
                                    "identifier": "Publish_Artifact",
                                    "spec": {
                                        "shell": "Bash",
                                        "command": "echo publish artifact placeholder"
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        },
        {
            "file": "Python_App_Deploy.yaml",
            "payload": {
                "template": {
                    "name": "Python App Deploy",
                    "identifier": "Python_App_Deploy",
                    "versionLabel": "v1",
                    "type": "StepGroup",
                    "orgIdentifier": org,
                    "projectIdentifier": proj,
                    "spec": {
                        "stageType": "Deployment",
                        "steps": [
                            {
                                "step": {
                                    "type": "Run",
                                    "name": "Create venv & Install",
                                    "identifier": "Python_Install",
                                    "spec": {
                                        "shell": "Bash",
                                        "command": "python -m venv venv && . venv/bin/activate && pip install -r requirements.txt"
                                    }
                                }
                            },
                            {
                                "step": {
                                    "type": "Run",
                                    "name": "Run App",
                                    "identifier": "Python_Run",
                                    "spec": {
                                        "shell": "Bash",
                                        "command": "echo start python app placeholder"
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        },
        {
            "file": "DotNet_Build_Publish.yaml",
            "payload": {
                "template": {
                    "name": ".NET Build & Publish",
                    "identifier": "DotNet_Build_Publish",
                    "versionLabel": "v1",
                    "type": "StepGroup",
                    "orgIdentifier": org,
                    "projectIdentifier": proj,
                    "spec": {
                        "stageType": "Deployment",
                        "steps": [
                            {
                                "step": {
                                    "type": "Run",
                                    "name": "dotnet restore & build",
                                    "identifier": "DotNet_Build",
                                    "spec": {
                                        "shell": "Bash",
                                        "command": "dotnet restore && dotnet build --configuration Release"
                                    }
                                }
                            },
                            {
                                "step": {
                                    "type": "Run",
                                    "name": "dotnet publish",
                                    "identifier": "DotNet_Publish",
                                    "spec": {
                                        "shell": "Bash",
                                        "command": "dotnet publish -c Release -o out"
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
    ]

    for item in stepgroups:
        write_yaml(os.path.join(tmpl_dir, item["file"]), item["payload"], org, proj)

    # 3) Template registry (optional matching, you can edit later)
    reg_dir = os.path.join(out_root, "common", ".harness", "templates")
    os.makedirs(reg_dir, exist_ok=True)
    registry_payload = {
        "templates": [
            {
                "name": "Java Gradle Build",
                "templateRef": "Java_Gradle_Build",
                "versionLabel": "v1",
                "type": "StepGroup",
                "match": {
                    "tags_any": ["language:java", "build:gradle"],
                    "any_regex": [r"\bgradle\b", r"\.jar\b", r"\.war\b"]
                }
            },
            {
                "name": "Python App Deploy",
                "templateRef": "Python_App_Deploy",
                "versionLabel": "v1",
                "type": "StepGroup",
                "match": {
                    "tags_any": ["language:python", "python:venv"],
                    "any_regex": [r"\bpython\b", r"flask", r"django"]
                }
            },
            {
                "name": ".NET Build & Publish",
                "templateRef": "DotNet_Build_Publish",
                "versionLabel": "v1",
                "type": "StepGroup",
                "match": {
                    "tags_any": ["runtime:dotnet", "dotnet"],
                    "any_regex": [r"\.csproj\b", r"\.sln\b", r"\bdotnet\b"]
                }
            }
        ]
    }
    write_yaml(os.path.join(reg_dir, "template-registry.yaml"), registry_payload, org, proj)

# ----------------------------- File collection -----------------------------
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

def ensure_harness_dirs(base_root: str) -> Tuple[str, str]:
    out_root = os.path.join(base_root, ".harness")
    services_dir = os.path.join(out_root, "services")
    pipelines_dir = os.path.join(out_root, "pipelines")
    os.makedirs(services_dir, exist_ok=True)
    os.makedirs(pipelines_dir, exist_ok=True)
    return services_dir, pipelines_dir

# ----------------------------- Main convert -----------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Convert UCD export JSON to Harness YAML (Services + Pipelines + Templates)")
    p.add_argument("--input", action="append", help="Path(s) to UCD JSON (repeatable or comma-separated).")
    p.add_argument("--input-dir", help="Directory containing UCD JSON files.")
    p.add_argument("--glob", default="*.json", help="Glob used with --input-dir (default: *.json)")
    p.add_argument("--recursive", action="store_true", help="Recurse into subfolders when using --input-dir")
    p.add_argument("--out", required=True, help="Output root folder")
    p.add_argument("--org", required=True, help="Harness orgIdentifier")
    p.add_argument("--project", required=True, help="Harness projectIdentifier")
    p.add_argument("--registry", default=None, help="Path to template registry YAML (optional). If omitted, uses the generated common registry.")
    p.add_argument("--first-match", action="store_true", help="Stop after first matching StepGroup per component")
    args = p.parse_args()

    # Emit common templates (import FIRST in Harness)
    write_common_templates(args.out, args.org, args.project, custom_deploy_identifier="Generic_Custom_Deployment")

    # Decide registry path (CLI over generated)
    registry_path = args.registry or os.path.join(args.out, "common", ".harness", "templates", "template-registry.yaml")
    registry = load_registry(registry_path)

    files = collect_input_files(args.input or [], args.input_dir, args.glob, args.recursive)
    if not files:
        print("ERROR: No input files found."); sys.exit(2)

    grand_apps = grand_svcs = 0
    for idx, path in enumerate(files, 1):
        print(f"\n[{idx}/{len(files)}] Processing: {path}")
        try:
            ucd = load_ucd_json(path)
        except Exception as e:
            print(f"  Skipping (bad JSON): {e}")
            continue

        for app in (ucd.get("applications") or []):
            app_meta = app.get("application") or {}
            app_name = app_meta.get("name") or "Application"
            app_tags_flat = collect_tags_flat(app_meta.get("tags") or [])
            app_tags_map  = collect_tags_map(app_meta.get("tags") or [])

            base_root = os.path.join(args.out, sanitize_identifier(app_name))
            services_dir, pipelines_dir = ensure_harness_dirs(base_root)

            stages: List[Dict[str, Any]] = []
            svc_count = 0

            for comp in (app.get("components") or []):
                comp_name = comp.get("name") or "Component"
                comp_tags_flat = collect_tags_flat(comp.get("tags") or [])
                comp_tags_map  = collect_tags_map(comp.get("tags") or [])
                tags_map = {**app_tags_map, **comp_tags_map}

                # unique service id: {App}_{Component}
                svc_identifier = sanitize_identifier(f"{app_name}_{comp_name}")

                # SERVICE (CustomDeployment + customDeploymentRef to our generated template)
                svc_yaml = build_service_payload(
                    name=comp_name,
                    identifier=svc_identifier,
                    tags_map=tags_map,
                    custom_deploy_ref="Generic_Custom_Deployment"
                )
                svc_path = os.path.join(services_dir, f"{svc_identifier}.yaml")
                write_yaml(svc_path, svc_yaml, args.org, args.project)
                svc_count += 1

                # Match StepGroups (optional)
                matched = match_stepgroups_for_component(app_name, comp_name, app_tags_flat, comp_tags_flat, registry, first_match=args.first_match)

                # STAGE
                stage_name = f"Deploy {comp_name}"
                stage = build_stage_for_component(svc_identifier, stage_name, matched)
                stages.append(stage)

            # PIPELINE (one per application)
            pipeline_name = f"{app_name} deploy"
            pipeline_id   = f"{app_name}_deploy"
            pipeline_yaml = build_pipeline_payload(pipeline_name, pipeline_id, args.org, args.project, stages, app_tags_map)
            pipe_path = os.path.join(pipelines_dir, f"{sanitize_identifier(pipeline_id)}.yaml")
            write_yaml(pipe_path, pipeline_yaml, args.org, args.project)

            grand_apps += 1
            grand_svcs += svc_count
            print(f"  Converted application: {app_name}  ->  {svc_count} services, 1 pipeline")

    print(f"\nAll done. Processed {len(files)} file(s), {grand_apps} applications, {grand_svcs} services.")
    print(f"Output root: {os.path.abspath(args.out)}")
    print("\nIMPORT ORDER in Harness (no Git):")
    print("  1) Templates: import common/.harness/templates/Generic_Custom_Deployment.yaml first,")
    print("                then any StepGroup templates you want to use.")
    print("  2) Services:  import each app/.harness/services/*.yaml")
    print("  3) Pipelines: import each app/.harness/pipelines/*.yaml")

if __name__ == "__main__":
    main()
