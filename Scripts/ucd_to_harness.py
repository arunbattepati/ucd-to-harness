#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UCD → Harness YAML Converter (import-ready; no manual edits required)

Output layout:
  <out>/
    common_templates/                 # shared templates (de-duped by identifier)
    <app>/
      services/
      environments/
      infrastructures/
      pipelines/
      input_sets/

Top-level YAML entity keys supported:
  - template
  - service
  - environment
  - infrastructureDefinition
  - pipeline
  - inputSet

Usage:
  python Scripts/ucd_to_harness_yaml_converter.py \
    --input-dir ucd_input_files \
    --out harness_out \
    --org default \
    --project ucd2harnessmigration \
    --group-by application
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
import argparse
import json
import re
import sys

try:
    import yaml  # PyYAML
except Exception as e:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    raise

# ---------------------------
# Config / constants
# ---------------------------
SUPPORTED_TOP_KEYS = {
    "template",
    "service",
    "environment",
    "infrastructureDefinition",
    "pipeline",
    "inputSet",
}

# ---------------------------
# Identifier utilities
# ---------------------------
_IDENT_RX_ALLOWED = re.compile(r"[^A-Za-z0-9_]")
_IDENT_RX_LEAD = re.compile(r"^[A-Za-z]")

def to_identifier(s: str) -> str:
    """Harness identifiers must contain only letters/numbers/underscore and start with a letter."""
    s = (s or "").strip()
    s = _IDENT_RX_ALLOWED.sub("_", s)
    s = re.sub(r"_+", "_", s)
    if not _IDENT_RX_LEAD.match(s):
        s = f"A_{s}" if s else "A_auto"
    return s

def to_safe_filename(*parts: str) -> str:
    segs = [to_identifier(p) for p in parts if p]
    base = "_".join(segs) or "artifact"
    return f"{base}.yaml"

# ---------------------------
# YAML writer
# ---------------------------
def yaml_write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not str(path).lower().endswith(".yaml"):
        path = path.with_suffix(".yaml")
    with path.open("w", encoding="utf-8") as f:
        f.write("---\n")
        yaml.safe_dump(obj, f, sort_keys=False)

# ---------------------------
# Writer orchestrating layout
# ---------------------------
class Writer:
    def __init__(self, out_root: Path) -> None:
        self.root = out_root
        self.common_dir = self.root / "common_templates"
        self.common_dir.mkdir(parents=True, exist_ok=True)
        self._template_id_seen: set[str] = set()

    def _app_dir(self, app: str) -> Path:
        safe = to_identifier(app.strip().lstrip(".").replace(" ", "_"))
        d = self.root / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_template(self, tpl: dict) -> Optional[Path]:
        t = tpl["template"]
        t["identifier"] = to_identifier(t.get("identifier") or t.get("name") or "Template")
        ident = t["identifier"]
        if ident in self._template_id_seen:
            return None
        self._template_id_seen.add(ident)
        out = self.common_dir / to_safe_filename(ident)
        yaml_write(out, {"template": t})
        return out

    def write_service(self, app: str, svc: dict) -> Path:
        s = svc["service"]
        s["identifier"] = to_identifier(s.get("identifier") or s.get("name") or f"{app}_service")
        out = self._app_dir(app) / "services" / to_safe_filename(app, s["identifier"])
        yaml_write(out, {"service": s})
        return out

    def write_environment(self, app: str, env: dict) -> Path:
        e = env["environment"]
        e["identifier"] = to_identifier(e.get("identifier") or e.get("name") or f"{app}_env")
        out = self._app_dir(app) / "environments" / to_safe_filename(app, e["identifier"])
        yaml_write(out, {"environment": e})
        return out

    def write_infrastructure(self, app: str, infra: dict) -> Path:
        key = "infrastructureDefinition"
        i = infra[key]
        i["identifier"] = to_identifier(i.get("identifier") or i.get("name") or f"{app}_infra")
        out = self._app_dir(app) / "infrastructures" / to_safe_filename(app, i["identifier"])
        yaml_write(out, {key: i})
        return out

    def write_pipeline(self, app: str, pipe: dict) -> Path:
        p = pipe["pipeline"]
        p["identifier"] = to_identifier(p.get("identifier") or p.get("name") or f"{app}_pipeline")
        out = self._app_dir(app) / "pipelines" / to_safe_filename(app, p["identifier"])
        yaml_write(out, {"pipeline": p})
        return out

    def write_input_set(self, app: str, input_set: dict) -> Path:
        i = input_set["inputSet"]
        i["identifier"] = to_identifier(i.get("identifier") or i.get("name") or f"{app}_inputs")
        out = self._app_dir(app) / "input_sets" / to_safe_filename(app, i["identifier"])
        yaml_write(out, {"inputSet": i})
        return out

# ---------------------------
# Normalization helpers
# ---------------------------
def ensure_org_project(entity: dict, org: str, project: str) -> None:
    """Inject orgIdentifier/projectIdentifier where applicable (no override)."""
    # Detect the sole top-level key
    keys = [k for k in entity.keys() if k in SUPPORTED_TOP_KEYS]
    if not keys:
        return
    k = keys[0]
    body = entity[k]
    if not isinstance(body, dict):
        return

    # Not all entity types require both, but setting both is safe
    if "orgIdentifier" not in body:
        body["orgIdentifier"] = org
    if "projectIdentifier" not in body and k != "template":  # templates can be org/project scoped; leave as-is if absent
        body["projectIdentifier"] = project

def normalize_identifiers(entity: dict, app: str) -> None:
    """Sanitize names and identifiers for imported entities."""
    keys = [k for k in entity.keys() if k in SUPPORTED_TOP_KEYS]
    if not keys:
        return
    k = keys[0]
    body = entity[k]
    if not isinstance(body, dict):
        return

    # id rules
    default_suffix = {
        "template": "Template",
        "service": "service",
        "environment": "env",
        "infrastructureDefinition": "infra",
        "pipeline": "pipeline",
        "inputSet": "inputs",
    }[k]

    body["identifier"] = to_identifier(body.get("identifier") or body.get("name") or f"{app}_{default_suffix}")

    # For templates ensure versionLabel
    if k == "template":
        body["versionLabel"] = body.get("versionLabel") or "v1"

# ---------------------------
# Input reading (dir/file; json/yaml)
# ---------------------------
def _load_one(p: Path) -> Union[List[dict], dict]:
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return data

def read_input_dir_or_file(path: Path) -> List[dict]:
    """
    Accepts:
      - directory containing multiple *.json/*.yaml files
      - single file (json/yaml) with either a list[dict] or a single dict
    Returns a list of "UCD units". Each unit should contain enough info to identify the application.
    """
    if not path.exists():
        raise FileNotFoundError(f"Input not found: {path}")

    raw_items: List[dict] = []
    if path.is_dir():
        files = sorted(list(path.glob("*.json")) + list(path.glob("*.yaml")) + list(path.glob("*.yml")))
        if not files:
            raise FileNotFoundError(f"No .json/.yaml files in {path}")
        for f in files:
            data = _load_one(f)
            if isinstance(data, list):
                raw_items.extend(data)
            elif isinstance(data, dict):
                raw_items.append(data)
            else:
                raise ValueError(f"Unsupported structure in {f}")
    else:
        data = _load_one(path)
        if isinstance(data, list):
            raw_items.extend(data)
        elif isinstance(data, dict):
            raw_items.append(data)
        else:
            raise ValueError(f"Unsupported structure in {path}")

    return raw_items

# ---------------------------
# UCD → Harness mapping (keeps your shapes if already Harness-like)
# ---------------------------
def map_ucd_unit(ucd: dict, org: str, project: str) -> Dict[str, Any]:
    """
    Returns:
      {
        "app": "<name>",
        "templates": [ {template: {...}}, ... ],
        "services":  [ {service: {...}}, ... ],
        "environments": [ {environment: {...}}, ... ],
        "infrastructures": [ {infrastructureDefinition: {...}}, ... ],
        "pipelines": [ {pipeline: {...}}, ... ],
        "input_sets": [ {inputSet: {...}}, ... ],
      }

    Notes:
    - If your UCD item already holds Harness-shaped dicts, we just pass and normalize.
    - If it holds simple dicts, we wrap them into correct top-level keys.
    - Tries to derive the application name from common fields.
    """
    # derive app name
    app = (
        ucd.get("app")
        or ucd.get("application")
        or ucd.get("applicationName")
        or ucd.get("name")
        or "application"
    )
    app = str(app)

    def wrap_list(key: str, items: List[dict], default_builder) -> List[dict]:
        out = []
        for it in items or []:
            if key in it:
                out.append(it)
            else:
                out.append(default_builder(it))
        return out

    # defaults to ensure shape if not already Harness-like
    templates = wrap_list(
        "template",
        ucd.get("templates", []),
        lambda t: {
            "template": {
                "name": t.get("name", "Template"),
                "identifier": t.get("identifier") or to_identifier(t.get("name", "Template")),
                "versionLabel": t.get("versionLabel", "v1"),
                "type": t.get("type", "Step"),
                "spec": t.get("spec", {}),
            }
        },
    )

    services = wrap_list(
        "service",
        ucd.get("services", []),
        lambda s: {
            "service": {
                "name": s.get("name", f"{app}-service"),
                "identifier": s.get("identifier") or f"{to_identifier(app)}_service",
                "orgIdentifier": s.get("orgIdentifier", org),
                "projectIdentifier": s.get("projectIdentifier", project),
                "serviceDefinition": s.get("serviceDefinition", {"type": "CustomDeployment", "spec": {}}),
            }
        },
    )

    environments = wrap_list(
        "environment",
        ucd.get("environments", []),
        lambda e: {
            "environment": {
                "name": e.get("name", f"{app}-env"),
                "identifier": e.get("identifier") or f"{to_identifier(app)}_env",
                "type": e.get("type", "PreProduction"),
                "orgIdentifier": e.get("orgIdentifier", org),
                "projectIdentifier": e.get("projectIdentifier", project),
            }
        },
    )

    infrastructures = wrap_list(
        "infrastructureDefinition",
        ucd.get("infrastructures", []),
        lambda i: {
            "infrastructureDefinition": {
                "name": i.get("name", f"{app}-infra"),
                "identifier": i.get("identifier") or f"{to_identifier(app)}_infra",
                "orgIdentifier": i.get("orgIdentifier", org),
                "projectIdentifier": i.get("projectIdentifier", project),
                "environmentRef": i.get("environmentRef") or f"{to_identifier(app)}_env",
                "type": i.get("type", "CustomDeployment"),
                "spec": i.get("spec", {"connectorRef": "<+input>", "namespace": "<+input>"}),
            }
        },
    )

    pipelines = wrap_list(
        "pipeline",
        ucd.get("pipelines", []),
        lambda p: {
            "pipeline": {
                "name": p.get("name", f"{app} deploy"),
                "identifier": p.get("identifier") or f"{to_identifier(app)}_deploy",
                "orgIdentifier": p.get("orgIdentifier", org),
                "projectIdentifier": p.get("projectIdentifier", project),
                "tags": p.get("tags", {}),
                "stages": p.get("stages", []),
            }
        },
    )

    input_sets = wrap_list(
        "inputSet",
        ucd.get("input_sets", []) or ucd.get("inputSets", []),
        lambda i: {
            "inputSet": {
                "name": i.get("name", f"{app} Inputs"),
                "identifier": i.get("identifier") or f"{to_identifier(app)}_inputs",
                "orgIdentifier": i.get("orgIdentifier", org),
                "projectIdentifier": i.get("projectIdentifier", project),
                "pipeline": {"identifier": i.get("pipelineIdentifier") or f"{to_identifier(app)}_deploy"},
                "pipelineInputs": i.get("pipelineInputs", {}),
            }
        },
    )

    # Normalize all entities (ids & org/project injection)
    all_lists = [templates, services, environments, infrastructures, pipelines, input_sets]
    for lst in all_lists:
        for entity in lst:
            normalize_identifiers(entity, app)
            ensure_org_project(entity, org, project)

    return {
        "app": app,
        "templates": templates,
        "services": services,
        "environments": environments,
        "infrastructures": infrastructures,
        "pipelines": pipelines,
        "input_sets": input_sets,
    }

# ---------------------------
# Conversion driver
# ---------------------------
def convert_all(ucd_units: Iterable[dict], writer: Writer, org: str, project: str) -> List[Tuple[str, Path]]:
    results: List[Tuple[str, Path]] = []
    for unit in ucd_units:
        mapped = map_ucd_unit(unit, org, project)
        app = mapped["app"]

        # Common templates (de-dup across apps)
        for tpl in mapped["templates"]:
            p = writer.write_template(tpl)
            if p:
                results.append(("template", p))

        # Per-app items
        for svc in mapped["services"]:
            results.append(("service", writer.write_service(app, svc)))
        for env in mapped["environments"]:
            results.append(("environment", writer.write_environment(app, env)))
        for infra in mapped["infrastructures"]:
            results.append(("infrastructure", writer.write_infrastructure(app, infra)))
        for pipe in mapped["pipelines"]:
            results.append(("pipeline", writer.write_pipeline(app, pipe)))
        for ins in mapped["input_sets"]:
            results.append(("input_set", writer.write_input_set(app, ins)))

    return results

# ---------------------------
# CLI
# ---------------------------
def main(argv: List[str]) -> None:
    ap = argparse.ArgumentParser(description="UCD → Harness YAML converter (import-ready)")
    ap.add_argument("--input-dir", required=True, help="Directory or file containing UCD JSON/YAML")
    ap.add_argument("--out", required=True, help="Output root (e.g., harness_out)")
    ap.add_argument("--org", default="default", help="Harness orgIdentifier (default: default)")
    ap.add_argument("--project", default="ucd2harnessmigration", help="Harness projectIdentifier (default: ucd2harnessmigration)")
    ap.add_argument("--group-by", default="application", help="Grouping mode (currently informational; default: application)")
    args = ap.parse_args(argv[1:])

    input_path = Path(args.input_dir)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    ucd_units = read_input_dir_or_file(input_path)
    writer = Writer(out_root)

    results = convert_all(ucd_units, writer, args.org, args.project)

    # Summary
    counts: Dict[str, int] = {}
    for kind, _ in results:
        counts[kind] = counts.get(kind, 0) + 1

    print(f"[OK] Wrote {len(results)} YAMLs to {out_root.resolve()}")
    if counts:
        print("Counts:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

if __name__ == "__main__":
    main(sys.argv)
