# UCD → Harness Accelerator

Convert IBM UrbanCode Deploy (UCD) **export JSON** into **Harness NG YAML** (services & pipelines), with:
- Clean, Harness-compliant YAML (names/identifiers sanitized, correct indentation).
- Auto-injection of `orgIdentifier`/`projectIdentifier`.
- Heuristics to pick `deploymentType` (`WinRm`, `TAS`, otherwise `Custom`).
- Optional **reusable StepGroup templates** via a local registry (`.harness/template-registry.yaml`).
- Multi-file input, directory sweeps, and organized outputs (`--group-by file|application|none`).

## Concept Mapping (UCD → Harness)

| Concept               | uDeploy JSON (IBM)                   | Harness YAML (Harness.io)                                |
| --------------------- | ------------------------------------ | -------------------------------------------------------- |
| **Process Name**      | `"name": "Deploy Component"`         | `pipeline: name: WebApp-Deployment`                      |
| **Steps**             | `"componentProcessStep"` objects     | `steps:` under `stage.spec.execution`                    |
| **Shell Command**     | `"command": "systemctl stop tomcat"` | `ShellScript` step → `spec.source.spec.script:`          |
| **Artifact Download** | `"Download Artifacts"` destination   | `DownloadArtifact` with `destinationPath`                |
| **Properties / Vars** | `${p:version/name}` placeholders     | `connectorRef`, `artifactRef`, env/pipeline variables    |
| **Tags**              | `"tags": ["deploy","webapp"]`        | `tags: { deploy: "webapp" }`                             |

---

## Requirements

- **Python 3.9+** (3.12 recommended)  
- **PyYAML**: `pip install pyyaml`

> The script validates written YAML by parsing it back; if a file is malformed, you’ll get a clear error on write.

---

## How to Use

### Install PyYAML
```bash
pip install pyyaml
```

### Run the converter (single file)
```bash
python Scripts/ucd_to_harness.py \
  --input ucd_input_files/ucd-demo.json \
  --out harness_out \
  --org my_org --project my_project
```

### Run on a whole folder (multi-file)
```bash
python Scripts/ucd_to_harness.py \
  --input-dir ucd_input_files \
  --recursive \
  --out harness_out \
  --org my_org --project my_project
```

### Control output layout
- Group by input **file** (default):
```bash
python Scripts/ucd_to_harness.py \
  --input-dir ucd_input_files \
  --out harness_out \
  --org my_org --project my_project \
  --group-by file
```

- Group by **application** name:
```bash
python Scripts/ucd_to_harness.py \
  --input-dir ucd_input_files \
  --out harness_out \
  --org my_org --project my_project \
  --group-by application
```

- Single combined tree (legacy):
```bash
python Scripts/ucd_to_harness.py \
  --input ucd_input_files/ucd-0822.json \
  --out harness_out \
  --org my_org --project my_project \
  --group-by none
```

### (Optional) Use reusable StepGroup templates
Create `.harness/template-registry.yaml` to auto-attach templates by tags/regex:

```yaml
templates:
  - name: Java Gradle Build
    templateRef: Java_Gradle_Build
    versionLabel: v1
    match:
      tags_any: ["build:gradle", "language:java"]
      any_regex: ["gradle|jar|war"]
    inputs:
      variables:
        buildArgs: "--no-daemon --stacktrace"

  - name: Windows IIS Deploy
    templateRef: Win_IIS_Deploy
    versionLabel: v2
    match:
      tags_any: ["IIS", "Windows_Service", "Windows-BATCH"]
```

> Add `--first-match` if you want only the first matching template per component.

---

## Output

**Default (group-by `file`)**
```
harness_out/
  ucd_demo/.harness/
    services/*.yaml              # one Service per UCD component
    pipelines/*_deploy.yaml      # one Pipeline per UCD application
```

**Each pipeline has**
- One **Deployment stage per component**
- Runtime inputs for `environmentRef` and `infrastructureDefinitions[0].identifier`  
  *(Pick Dev/QA/Prod + infra at run time)*

---

## What Happens During Conversion (Step-by-Step)

1. **Read UCD JSON**  
   Parses `applications[*].application.name` and each `components[*].name/tags`.

2. **Create one Harness Service per UCD component**  
   - File: `.../.harness/services/<App>_<Component>.yaml`  
   - Carries combined UCD tags (app + component) for search/governance.  
   - Names/identifiers sanitized to Harness regex.

3. **Choose a Harness `deploymentType` per component** *(heuristic)*  
   - **WinRm** if it looks Windows/IIS/MSI/COM.  
   - **TAS** if PCF/Tanzu/TAS is detected.  
   - Otherwise **Custom** (safe fallback).  
   - Unknown values are normalized (e.g., `pcf` → `TAS`).

4. **Match reusable templates via registry (optional)**  
   For each component, the script builds a haystack from:
   `app name + component name + app tags + component tags`.  
   It checks `.harness/template-registry.yaml` with:
   - `tags_any` / `tags_all` against tokenized UCD tags (key:value or plain).
   - `any_regex` / `all_regex` against names/tags text.  
   Each matching template is injected as a `stepGroup` in order.

5. **Build a Deployment stage per component**  
   Stage includes:
   - `serviceRef: <App>_<Component>`  
   - `environmentRef` & `infrastructureDefinitions[0].identifier` = `<+input>`  
   - `execution.steps`: injected `stepGroup`(s) + a final `ShellScript` placeholder  
   - A default `failureStrategies: StageRollback` for safe behavior.

6. **Write the Pipeline (one per application)**  
   - File: `.../.harness/pipelines/<app>_deploy.yaml`  
   - Contains all component stages with injected template calls.

7. **Import/Run in Harness**  
   Harness resolves each `stepGroup.template.templateRef` to the actual Template in your org/project and `versionLabel`.  
   If a StepGroup variable value is `<+input>`, you’ll be prompted at run time (or set upstream).

---

## Formatting & Compliance Fixes (Built-In)

- **Identifier & Name Sanitization**  
  Enforces Harness regex: identifiers `^[A-Za-z_][0-9A-Za-z_]{0,127}$`; names stripped of `/, \, (, )` and normalized to max length.

- **Valid `deploymentType`**  
  Normalizes synonyms and defaults to `Custom` to prevent import errors.

- **Org/Project Metadata**  
  Auto-injects `orgIdentifier` and `projectIdentifier` on pipelines/services (and other top-levels the script emits).

- **YAML Validation**  
  Every YAML file is re-parsed after writing; malformed YAML fails fast with a clear message.

- **Multi-file & Grouped Output**  
  Supports multiple `--input`, `--input-dir`, `--recursive`, and `--group-by` for neat separation.

---

## Previously Used Gradle Flags (Deprecated)

Older examples passed Gradle template refs via CLI:
```bash
python Scripts/ucd_to_harness.py \
  --input ucd_input_files/ucd-0822.json \
  --out harness_out \
  --org my_org --project my_project \
  --gradle-template-ref Java_Gradle_Build \
  --gradle-template-version v1 \
  --gradle-match "java|gradle|jar|war"
```

**Now replaced** by the **template registry** (`.harness/template-registry.yaml`) shown above—more scalable and maintainable for multiple technologies.

---

## License

MIT (or match your repo’s license).
