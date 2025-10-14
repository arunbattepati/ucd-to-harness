#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UCD → Harness YAML Converter (import-ready; no manual edits required)

Key features:
- Recursively reads input dir (supports nested folders).
- Accepts many UCD shapes: {"applications":[...]}, {"Applications":[...]}, {"ucdExport":{...}},
  or single-entity files (pipeline/template/service/environment/infrastructureDefinition/inputSet).
- Outputs to:
    <out>/common_templates/
    <out>/<app>/{services,environments,infrastructures,pipelines,input_sets}/
- Ensures Harness-importable YAML (--- header, .yaml extension, sanitized identifiers).
- Adds --verbose to print per-file stats & warnings.

Usage:
  python Scripts/ucd_to_harness_yaml_converter.py \
    --input-dir ucd_input_files \
    --out harness_out \
    --org default \
    --project ucd2harnessmigration \
    --group-by application \
    --verbose
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
except Exception:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    raise

SUPPORTED_TOP_KEYS = {
    "template",
    "service",
    "environment",
    "infrastructureDefinition",
    "pipeline",
    "inputSet",
}

_IDENT_RX_ALLOWED = re.compile(r"[^A-Za-z0-9_]")
_IDENT_RX_LEAD = re.compile(r"^[A-Za-z]")

def to_identifier(s: str) -> str:
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

def yaml_write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not str(path).lower().endswith(".yaml"):
        path = path.with_suffix(".yaml")
    with path.open("w", encoding="utf-8") as f:
        f.write("---\n")
        yaml.safe_dump(obj, f, sort_keys=False)

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

def ensure_org_project(entity: dict, org: str, project: str) -> None:
    keys = [k for k in entity.keys() if k in SUPPORTED_TOP_KEYS]
    if not keys:
        return
    k = keys[0]
    body = entity[k]
    if not isinstance(body, dict):
        return
    if "orgIdentifier" not in body:
        body["orgIdentifier"] = org
    if "projectIdentifier" not in body and k != "template":
        body["projectIdentifier"] = project

def normalize_identifiers(entity: dict, app: str) -> None:
    keys = [k for k in entity.keys() if k in SUPPORTED_TOP_KEYS]
    if not keys:
        return
    k = keys[0]
    body = entity[k]
    if not isinstance(body, dict):
        return
    default_suffix = {
        "template": "Template",
        "service": "service",
        "environment": "env",
        "infrastructureDefinition": "infra",
        "pipeline": "pipeline",
        "inputSet": "inputs",
    }[k]
    body["identifier"] = to_identifier(body.get("identifier") or body.get("name") or f"{app}_{default_suffix}")
    if k == "template":
        body["versionLabel"] = body.get("versionLabel") or "v1"

def _load_one(p: Path) -> Union[List[dict], dict]:
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return data

def _explode_known_ucd_shapes(obj: Any, verbose: bool, src: Path) -> List[dict]:
    """
    Convert various UCD export shapes into a list of 'units' (each roughly one application).
    Supported patterns:
      - {"applications":[{...},{...}]}
      - {"Applications":[{...},{...}]}
      - {"ucdExport":{"applications":[...]}} or similar
      - a single entity dict with a top-level supported key (treated as one app)
      - already a "unit-like" dict (has app/application/applicationName/name + lists)
    """
    units: List[dict] = []

    def _is_entity_dict(d: dict) -> bool:
        return any(k in d for k in SUPPORTED_TOP_KEYS)

    if isinstance(obj, list):
        # Could be already a list of units or a list of entities
        # If items look like top-level entities, wrap as one unit named 'application'
        if obj and all(isinstance(x, dict) and _is_entity_dict(x) for x in obj):
            units.append({"app": "application", "entities": obj})
            return units
        # Otherwise treat each as a unit (best-effort)
        for x in obj:
            if isinstance(x, dict):
                units.append(x)
        return units

    if not isinstance(obj, dict):
        return units

    # Direct applications key (lower/upper)
    for key in ("applications", "Applications"):
        if key in obj and isinstance(obj[key], list):
            for app_item in obj[key]:
                if isinstance(app_item, dict):
                    units.append(app_item)
            return units

    # Nested export object
    if "ucdExport" in obj and isinstance(obj["ucdExport"], dict):
        nested = obj["ucdExport"]
        for key in ("applications", "Applications"):
            if key in nested and isinstance(nested[key], list):
                for app_item in nested[key]:
                    if isinstance(app_item, dict):
                        units.append(app_item)
                return units
        # fallback: treat nested dict itself as unit
        units.append(nested)
        return units

    # Single-entity file (e.g., only a pipeline)
    if _is_entity_dict(obj):
        units.append({"app": obj.get("applicationName") or obj.get("name") or "application", "entities": [obj]})
        return units

    # Default: assume already a unit-ish dict
    units.append(obj)
    return units

def read_input_dir_or_file(path: Path, verbose: bool) -> Tuple[List[dict], List[Tuple[Path, int]]]:
    """
    Returns (units, per_file_counts) where per_file_counts holds (#units derived) for logging.
    Recursively scans directories for *.json/*.yaml/*.yml.
    """
    if not path.exists():
        raise FileNotFoundError(f"Input not found: {path}")

    files: List[Path] = []
    if path.is_dir():
        files = sorted(list(path.rglob("*.json")) + list(path.rglob("*.yaml")) + list(path.rglob("*.yml")))
        if not files:
            raise FileNotFoundError(f"No .json/.yaml files found recursively under {path}")
    else:
        files = [path]

    units: List[dict] = []
    per_file_counts: List[Tuple[Path, int]] = []

    for f in files:
        try:
            data = _load_one(f)
            exploded = _explode_known_ucd_shapes(data, verbose, f)
            units.extend(exploded)
            per_file_counts.append((f, len(exploded)))
        except Exception as e:
            per_file_counts.append((f, 0))
            if verbose:
                print(f"[WARN] Skipped {f}: {e}", file=sys.stderr)

    return units, per_file_counts

def map_ucd_unit(ucd: dict, org: str, project: str) -> Dict[str, Any]:
    app = (
        ucd.get("app")
        or ucd.get("application")
        or ucd.get("applicationName")
        or ucd.get("name")
        or "application"
    )
    app = str(app)

    # Some shapes may place raw entity dicts under 'entities'
    entities_from_flat: List[dict] = []
    if isinstance(ucd.get("entities"), list):
        for e in ucd["entities"]:
            if isinstance(e, dict) and any(k in e for k in SUPPORTED_TOP_KEYS):
                entities_from_flat.append(e)

    def wrap_list(key: str, items: List[dict], default_builder) -> List[dict]:
        out = []
        for it in items or []:
            if key in it:
                out.append(it)
            else:
                out.append(default_builder(it))
        return out

    # Prefer explicit lists if present, otherwise mine from flat entities
    raw_tmpls = ucd.get("templates", [])
    raw_svcs = ucd.get("services", [])
    raw_envs = ucd.get("environments", [])
    raw_infras = ucd.get("infrastructures", [])
    raw_pipes = ucd.get("pipelines", [])
    raw_inputs = ucd.get("input_sets", []) or ucd.get("inputSets", [])

    # Sweep flat entities into respective buckets if not already populated
    if entities_from_flat:
        for ent in entities_from_flat:
            for k in SUPPORTED_TOP_KEYS:
                if k in ent:
                    if k == "template":
                        raw_tmpls.append(ent); break
                    if k == "service":
                        raw_svcs.append(ent); break
                    if k == "environment":
                        raw_envs.append(ent); break
                    if k == "infrastructureDefinition":
                        raw_infras.append(ent); break
                    if k == "pipeline":
                        raw_pipes.append(ent); break
                    if k == "inputSet":
                        raw_inputs.append(ent); break

    templates = wrap_list(
        "template",
        raw_tmpls,
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
        raw_svcs,
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
        raw_envs,
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
        raw_infras,
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
        raw_pipes,
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
        raw_inputs,
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

    # Normalize & inject org/project
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

def convert_all(ucd_units: Iterable[dict], writer: Writer, org: str, project: str) -> List[Tuple[str, Path]]:
    results: List[Tuple[str, Path]] = []
    for unit in ucd_units:
        mapped = map_ucd_unit(unit, org, project)
        app = mapped["app"]

        for tpl in mapped["templates"]:
            p = writer.write_template(tpl)
            if p:
                results.append(("template", p))
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

def main(argv: List[str]) -> None:
    ap = argparse.ArgumentParser(description="UCD → Harness YAML converter (import-ready; recursive input scanning)")
    ap.add_argument("--input-dir", required=True, help="Directory or file containing UCD JSON/YAML (scanned recursively for dirs)")
    ap.add_argument("--out", required=True, help="Output root (e.g., harness_out)")
    ap.add_argument("--org", default="default", help="Harness orgIdentifier (default: default)")
    ap.add_argument("--project", default="ucd2harnessmigration", help="Harness projectIdentifier (default: ucd2harnessmigration)")
    ap.add_argument("--group-by", default="application", help="Grouping mode (currently informational; default: application)")
    ap.add_argument("--verbose", action="store_true", help="Print per-file diagnostics")
    args = ap.parse_args(argv[1:])

    input_path = Path(args.input_dir)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    ucd_units, per_file_counts = read_input_dir_or_file(input_path, args.verbose)
    if args.verbose:
        for f, n in per_file_counts:
            print(f"[scan] {f} -> {n} unit(s)")

    writer = Writer(out_root)
    results = convert_all(ucd_units, writer, args.org, args.project)

    counts: Dict[str, int] = {}
    for kind, _ in results:
        counts[kind] = counts.get(kind, 0) + 1

    total = len(results)
    print(f"[OK] Wrote {total} YAMLs to {out_root.resolve()}")
    if counts:
        print("Counts:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if total == 0:
        print(
            "[HINT] No entities were emitted.\n"
            "  • Ensure your files contain at least one of: template/service/environment/infrastructureDefinition/pipeline/inputSet\n"
            "  • If your export is nested (e.g., {'ucdExport': {'applications': [...]}}), this script now handles it.\n"
            "  • Use --verbose to see how each file was interpreted.\n"
            "  • Verify --input-dir points to the folder/files with UCD exports (this script scans recursively).",
            file=sys.stderr,
        )

if __name__ == "__main__":
    main(sys.argv)
