#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UCD → Harness NG YAML Converter (services + pipelines + common templates; NO .harness folders)

- Python 3.9+ compatible.
- Accepts multiple --input files and/or --input-dir (with optional --recursive).
- Groups output per application:  <out>/<ApplicationName>/{services,pipelines}
- Common templates live at:       <out>/common_templates/{custom-deployments,step-groups,stages}/
- Generates:
  * Service YAML (CustomDeployment, with customDeploymentRef → Generic_Custom_Deployment)
  * Pipeline YAML (Deployment stages are CustomDeployment and always include 1 FetchInstanceScript step at top)
  * Common templates:
      - Custom Deployment template: custom-deployments/Generic_Custom_Deployment.yaml
      - Example StepGroup templates (step-groups/*.yaml)
      - Example Stage templates (stages/*.yaml)
      - Template registry: common_templates/template-registry.yaml

USAGE
-----
# Convert a directory of UCD exports
python Scripts/ucd_to_harness_yaml_converter.py \
  --input-dir ucd_input_files --recursive \
  --out harness_out --org default --project ucd2harnessmigration

# Convert explicit file(s)
python Scripts/ucd_to_harness_yaml_converter.py \
  --input ucd_input_files/ucd-0822.json,ucd_input_files/ucd-demo.yaml \
  --out harness_out --org default --project ucd2harnessmigration
"""
import os, re, sys, json, glob, argparse
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import yaml
except Exception:
    print("PyYAML is required. Install with:  pip install pyyaml")
    raise

# ----------------------------- constants -----------------------------
CUSTOM_DEPLOY_REF = "Generic_Custom_Deployment"
FETCH_STEP_TEMPLATE_ID = "Fetch_Instances"
FETCH_STEP_TEMPLATE_NAME = "Fetch Instances"
FETCH_STEP_TEMPLATE_VERSION = "v1"

# ----------------------------- validators -----------------------------
ID_RE  = re.compile(r"^[A-Za-z_][0-9A-Za-z_]{0,127}$")
NM_RE  = re.compile(r"^[A-Za-z_][-0-9A-Za-z_\\s]{0,127}$")
ID_SAN = re.compile(r"[^0-9A-Za-z_]+")

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
    s = (s or "Name")
    s = re.sub(r"[^0-9A-Za-z_\-\s.]+", " ", s)
    s = s.replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    if not s or not re.match(r"[A-Za-z_]", s[0]):
        s = "_" + s
    s = re.sub(r"[^-0-9A-Za-z_\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()[:128]
    if not NM_RE.match(s):
        s = "_" + re.sub(r"[^-0-9A-Za-z_\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()[:128]
    return s

def _walk_fix_ids_and_names(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k == "identifier" and isinstance(v, str) and not ID_RE.match(v):
                obj[k] = sanitize_identifier(v)
            if k == "name" and isinstance(v, str):
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
    if kind != "template":
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
    for top in ("pipeline", "service", "template"):
        if top in payload:
            ensure_meta(payload, top, org, proj)
    _walk_fix_ids_and_names(payload)
    text = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    # quick parse validation
    with open(path, "r", encoding="utf-8") as f:
        yaml.safe_load(f)

# ----------------------------- input helpers -----------------------------
def load_ucd(path: str) -> Union[Dict[str, Any], List[Any]]:
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read()
    if path.lower().endswith((".yaml", ".yml")):
        return yaml.safe_load(txt)
    return json.loads(txt)

def collect_input_files(inputs: List[str], input_dir: Optional[str], recursive: bool) -> List[str]:
    files: List[str] = []
    def _expand_dir(d: str) -> None:
        files.extend(glob.glob(os.path.join(d, "**", "*.json"), recursive=recursive))
        files.extend(glob.glob(os.path.join(d, "**", "*.y*ml"),  recursive=recursive))
    for item in inputs or []:
        for part in [p.strip() for p in item.split(",") if p.strip()]:
            if os.path.isdir(part): _expand_dir(part)
            else: files.append(part)
    if input_dir:
        if os.path.isdir(input_dir): _expand_dir(input_dir)
        else: files.append(input_dir)
    normed, seen = [], set()
    for f in files:
        p = os.path.abspath(f)
        if os.path.isfile(p) and (p.endswith(".json") or p.lower().endswith((".yaml",".yml"))) and p not in seen:
            seen.add(p); normed.append(p)
    return normed

def extract_applications(ucd_obj: Union[Dict[str, Any], List[Any]]) -> List[Dict[str, Any]]:
    if isinstance(ucd_obj, list):  # list of apps
        return [x for x in ucd_obj if isinstance(x, dict)]
    if not isinstance(ucd_obj, dict):
        return []
    for key in ("applications","Applications"):
        if key in ucd_obj and isinstance(ucd_obj[key], list):
            return [x for x in ucd_obj[key] if isinstance(x, dict)]
    if "ucdExport" in ucd_obj and isinstance(ucd_obj["ucdExport"], dict):
        exp = ucd_obj["ucdExport"]
        for key in ("applications","Applications"):
            if key in exp and isinstance(exp[key], list):
                return [x for x in exp[key] if isinstance(x, dict)]
    if "application" in ucd_obj or "components" in ucd_obj:
        return [ucd_obj]
    return []

# ----------------------------- tags helpers -----------------------------
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

# ----------------------------- registry -----------------------------
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

# ----------------------------- builders -----------------------------
def build_service_payload(name: str, identifier: str, tags_map: Dict[str, str]) -> Dict[str, Any]:
    return {
        "service": {
            "name": sanitize_name(name),
            "identifier": sanitize_identifier(identifier),
            "tags": tags_map or {},
            "serviceDefinition": {
                "type": "CustomDeployment",
                "spec": {
                    "customDeploymentRef": CUSTOM_DEPLOY_REF,
                    "variables": []
                }
            }
        }
    }

def build_stage_for_component(svc_identifier: str,
                              stage_name: str,
                              matched_stepgroups: List[Dict[str, Any]]) -> Dict[str, Any]:
    stage_disp_name = sanitize_name(stage_name)
    stage_ident = sanitize_identifier(stage_disp_name)
    stage = {
        "stage": {
            "name": stage_disp_name,
            "identifier": stage_ident,
            "type": "Deployment",
            "spec": {
                "deploymentType": "CustomDeployment",
                "infrastructure": {
                    "environmentRef": "<+input>",
                    "infrastructureDefinition": {
                        "type": "CustomDeployment",
                        "spec": {
                            "customDeploymentRef": CUSTOM_DEPLOY_REF,
                            "variables": []
                        }
                    },
                    "allowSimultaneousDeployments": True
                },
                "service": { "serviceRef": svc_identifier },
                "execution": { "steps": [] }
            },
            "failureStrategies": [
                { "onFailure": { "errors": ["AllErrors"], "action": {"type": "StageRollback"} } }
            ]
        }
    }
    steps = stage["stage"]["spec"]["execution"]["steps"]

    # StepTemplateRef (required by schema if using templates at step level)
    steps.append({
        "step": {
            "name": FETCH_STEP_TEMPLATE_NAME,
            "identifier": FETCH_STEP_TEMPLATE_ID,
            "template": {
                "templateRef": FETCH_STEP_TEMPLATE_ID,
                "versionLabel": FETCH_STEP_TEMPLATE_VERSION
            }
        }
    })

    # Matched StepGroup template refs
    for sg in matched_stepgroups or []:
        sg_name = sanitize_name(sg["name"]); sg_ident = sanitize_identifier(sg_name)
        block: Dict[str, Any] = {
            "stepGroup": {
                "name": sg_name,
                "identifier": sg_ident,
                "template": {
                    "templateRef": sg["templateRef"],
                    "versionLabel": sg.get("versionLabel", "v1")
                }
            }
        }
        if "templateInputs" in sg:
            block["stepGroup"]["template"]["templateInputs"] = sg["templateInputs"]
        steps.append(block)

    # Tail placeholder (inline step is fine after the template ref)
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

# ----------------------------- templates emission -----------------------------
def write_common_templates(out_root: str, org: str, proj: str) -> str:
    base = os.path.join(out_root, "common_templates")
    cd_dir     = os.path.join(base, "custom-deployments")
    steps_dir  = os.path.join(base, "steps")
    sg_dir     = os.path.join(base, "step-groups")
    stages_dir = os.path.join(base, "stages")
    for d in (cd_dir, steps_dir, sg_dir, stages_dir):
        os.makedirs(d, exist_ok=True)

    # 1) Custom Deployment Template
    cdt = {
        "template": {
            "name": "Generic Custom Deployment",
            "identifier": CUSTOM_DEPLOY_REF,
            "versionLabel": "v1",
            "type": "CustomDeployment",
            "orgIdentifier": org,
            "projectIdentifier": proj,
            "spec": {
                "infrastructure": { "variables": [] },
                "variables": []
            }
        }
    }
    write_yaml(os.path.join(cd_dir, f"{CUSTOM_DEPLOY_REF}.yaml"), cdt, org, proj)

    # 2) Step template: Fetch Instances (so Stage schema gets a StepTemplateRef)
    fetch_step_tmpl = {
        "template": {
            "name": FETCH_STEP_TEMPLATE_NAME,
            "identifier": FETCH_STEP_TEMPLATE_ID,
            "versionLabel": FETCH_STEP_TEMPLATE_VERSION,
            "type": "Step",
            "orgIdentifier": org,
            "projectIdentifier": proj,
            "spec": {
                "type": "FetchInstanceScript",
                "timeout": "10m",
                "spec": {
                    "shell": "Bash",
                    "onDelegate": True,
                    "source": {
                        "type": "Inline",
                        "spec": {
                            "script": (
                                'echo "Discovering instances for $HARNESS_SERVICE_NAME"\n'
                                'echo \'{"instances":[{"name":"sample-instance","id":"1"}]}\'\n'
                            )
                        }
                    }
                }
            }
        }
    }
    write_yaml(os.path.join(steps_dir, f"{FETCH_STEP_TEMPLATE_ID}.yaml"), fetch_step_tmpl, org, proj)

    # 3) StepGroup templates (stageType aligned to CustomDeployment)
    stepgroups = [
        ("Java_Gradle_Build.yaml", {
            "template": {
                "name": "Java Gradle Build",
                "identifier": "Java_Gradle_Build",
                "versionLabel": "v1",
                "type": "StepGroup",
                "orgIdentifier": org,
                "projectIdentifier": proj,
                "spec": {
                    "stageType": "CustomDeployment",
                    "steps": [
                        {"step": {"type":"Run","name":"Gradle Build","identifier":"Gradle_Build",
                                  "spec":{"shell":"Bash","command":"gradle clean build"}}},
                        {"step": {"type":"Run","name":"Publish Artifact","identifier":"Publish_Artifact",
                                  "spec":{"shell":"Bash","command":"echo publish artifact placeholder"}}}
                    ]
                }
            }
        }),
        ("Python_App_Deploy.yaml", {
            "template": {
                "name": "Python App Deploy",
                "identifier": "Python_App_Deploy",
                "versionLabel": "v1",
                "type": "StepGroup",
                "orgIdentifier": org,
                "projectIdentifier": proj,
                "spec": {
                    "stageType": "CustomDeployment",
                    "steps": [
                        {"step":{"type":"Run","name":"Create venv & Install","identifier":"Python_Install",
                                 "spec":{"shell":"Bash","command":"python -m venv venv && . venv/bin/activate && pip install -r requirements.txt"}}},
                        {"step":{"type":"Run","name":"Run App","identifier":"Python_Run",
                                 "spec":{"shell":"Bash","command":"echo start python app placeholder"}}}
                    ]
                }
            }
        }),
        ("DotNet_Build_Publish.yaml", {
            "template": {
                "name": ".NET Build & Publish",
                "identifier": "DotNet_Build_Publish",
                "versionLabel": "v1",
                "type": "StepGroup",
                "orgIdentifier": org,
                "projectIdentifier": proj,
                "spec": {
                    "stageType": "CustomDeployment",
                    "steps": [
                        {"step":{"type":"Run","name":"dotnet restore & build","identifier":"DotNet_Build",
                                 "spec":{"shell":"Bash","command":"dotnet restore && dotnet build --configuration Release"}}},
                        {"step":{"type":"Run","name":"dotnet publish","identifier":"DotNet_Publish",
                                 "spec":{"shell":"Bash","command":"dotnet publish -c Release -o out"}}}
                    ]
                }
            }
        })
    ]
    for fname, payload in stepgroups:
        write_yaml(os.path.join(sg_dir, fname), payload, org, proj)

    # 4) Stage template (Deployment, with failureStrategies and StepTemplateRef first)
    stage_template = {
        "template": {
            "name": "Generic Custom Deployment Stage",
            "identifier": "Generic_Custom_Deployment_Stage",
            "versionLabel": "v1",
            "type": "Stage",
            "orgIdentifier": org,
            "projectIdentifier": proj,
            "spec": {
                "type": "Deployment",
                "failureStrategies": [
                    {
                        "onFailure": {
                            "errors": ["AllErrors"],
                            "action": {"type": "StageRollback"}
                        }
                    }
                ],
                "spec": {
                    "deploymentType": "CustomDeployment",
                    "service": {"serviceRef": "<+input>"},
                    "infrastructure": {
                        "environmentRef": "<+input>",
                        "infrastructureDefinition": {
                            "type": "CustomDeployment",
                            "spec": {
                                "customDeploymentRef": CUSTOM_DEPLOY_REF,
                                "variables": []
                            }
                        },
                        "allowSimultaneousDeployments": True
                    },
                    "execution": {
                        "steps": [
                            {
                                "step": {
                                    "name": FETCH_STEP_TEMPLATE_NAME,
                                    "identifier": FETCH_STEP_TEMPLATE_ID,
                                    "template": {
                                        "templateRef": FETCH_STEP_TEMPLATE_ID,
                                        "versionLabel": FETCH_STEP_TEMPLATE_VERSION
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
    }
    write_yaml(os.path.join(stages_dir, "Generic_Custom_Deployment_Stage.yaml"), stage_template, org, proj)

    # 5) Template registry (for matching StepGroups)
    registry_payload = {
        "templates": [
            {"name":"Java Gradle Build","templateRef":"Java_Gradle_Build","versionLabel":"v1","type":"StepGroup",
             "match":{"tags_any":["language:java","build:gradle"],"any_regex":[r"\bgradle\b", r"\.jar\b", r"\.war\b"]}},
            {"name":"Python App Deploy","templateRef":"Python_App_Deploy","versionLabel":"v1","type":"StepGroup",
             "match":{"tags_any":["language:python","python:venv"],"any_regex":[r"\bpython\b","flask","django"]}},
            {"name":".NET Build & Publish","templateRef":"DotNet_Build_Publish","versionLabel":"v1","type":"StepGroup",
             "match":{"tags_any":["runtime:dotnet","dotnet"],"any_regex":[r"\.csproj\b", r"\.sln\b", r"\bdotnet\b"]}}
        ]
    }
    reg_path = os.path.join(base, "template-registry.yaml")
    write_yaml(reg_path, registry_payload, org, proj)
    return reg_path

# ----------------------------- app dirs -----------------------------
def ensure_app_dirs(base_root: str) -> Tuple[str, str]:
    services_dir  = os.path.join(base_root, "services")
    pipelines_dir = os.path.join(base_root, "pipelines")
    os.makedirs(services_dir, exist_ok=True)
    os.makedirs(pipelines_dir, exist_ok=True)
    return services_dir, pipelines_dir

# ----------------------------- main -----------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Convert UCD export JSON/YAML to Harness YAML (forward-fix)")
    p.add_argument("--input", action="append", help="Path(s) to UCD files (repeatable or comma-separated).")
    p.add_argument("--input-dir", help="Directory containing UCD files.")
    p.add_argument("--recursive", action="store_true", help="Recurse into subfolders when using --input-dir")
    p.add_argument("--out", required=True, help="Output root folder (e.g., harness_out)")
    p.add_argument("--org", required=True, help="Harness orgIdentifier")
    p.add_argument("--project", required=True, help="Harness projectIdentifier")
    p.add_argument("--registry", default=None, help="Path to template registry YAML (optional). If omitted, uses generated common_templates registry.")
    p.add_argument("--first-match", action="store_true", help="Stop after first matching StepGroup per component")
    args = p.parse_args()

    # Seed common templates and load registry
    generated_reg_path = write_common_templates(args.out, args.org, args.project)
    registry_path = args.registry or generated_reg_path
    registry = load_registry(registry_path)

    files = collect_input_files(args.input or [], args.input_dir, args.recursive)
    if not files:
        print("ERROR: No input files found."); sys.exit(2)

    grand_apps = grand_svcs = 0
    for idx, path in enumerate(files, 1):
        print(f"\n[{idx}/{len(files)}] Processing: {path}")
        try:
            ucd_obj = load_ucd(path)
        except Exception as e:
            print(f"  Skipping (bad file): {e}")
            continue

        apps = extract_applications(ucd_obj)
        if not apps:
            print("  [warn] No applications found in this file.")
            continue

        for app in apps:
            app_meta = app.get("application") or {}
            app_name = app_meta.get("name") or app.get("applicationName") or app.get("name") or "Application"
            app_tags_flat = collect_tags_flat(app_meta.get("tags") or [])
            app_tags_map  = collect_tags_map(app_meta.get("tags") or [])

            base_root = os.path.join(args.out, sanitize_identifier(app_name))
            services_dir, pipelines_dir = ensure_app_dirs(base_root)

            stages: List[Dict[str, Any]] = []
            svc_count = 0

            comps = app.get("components") or []
            if not comps:
                print(f"  [warn] Application '{app_name}' has no components; pipeline will have no stages.")

            for comp in comps:
                comp_name = comp.get("name") or "Component"
                comp_tags_flat = collect_tags_flat(comp.get("tags") or [])
                comp_tags_map  = collect_tags_map(comp.get("tags") or [])
                tags_map = {**app_tags_map, **comp_tags_map}

                svc_identifier = sanitize_identifier(f"{app_name}_{comp_name}")

                # SERVICE
                svc_yaml = build_service_payload(comp_name, svc_identifier, tags_map)
                svc_path = os.path.join(services_dir, f"{svc_identifier}.yaml")
                write_yaml(svc_path, svc_yaml, args.org, args.project)
                svc_count += 1

                # Matched StepGroups (optional)
                matched = match_stepgroups_for_component(app_name, comp_name, app_tags_flat, comp_tags_flat, registry, first_match=args.first_match)

                # STAGE
                stage_name = f"Deploy {comp_name}"
                stages.append(build_stage_for_component(svc_identifier, stage_name, matched))

            # PIPELINE
            pipeline_name = f"{app_name} deploy"
            pipeline_id   = f"{app_name}_deploy"
            pipe_path = os.path.join(pipelines_dir, f"{sanitize_identifier(pipeline_id)}.yaml")
            pipeline_yaml = build_pipeline_payload(pipeline_name, pipeline_id, args.org, args.project, stages, app_tags_map)
            write_yaml(pipe_path, pipeline_yaml, args.org, args.project)

            grand_apps += 1
            grand_svcs += svc_count
            print(f"  Converted application: {app_name}  ->  {svc_count} services, 1 pipeline")

    print(f"\nAll done. Processed {len(files)} file(s), {grand_apps} applications, {grand_svcs} services.")
    print(f"Output root: {os.path.abspath(args.out)}")
    print("\nIMPORT ORDER in Harness:")
    print("  1) Templates: common_templates/custom-deployments/, steps/, step-groups/, stages/")
    print("  2) Services:  <App>/services/*.yaml")
    print("  3) Pipelines: <App>/pipelines/*.yaml")

if __name__ == "__main__":
    main()
