# Roboflow Local Demo Bundle

This bundle is the public-safe subset of the Roboflow local demo flow from the AitherOS monorepo.

## Included

- `workflow/asset_pipeline.json` — validated local-compatible workflow
- `scripts/start_inference.ps1` — start the local Roboflow inference container
- `scripts/health_check.ps1` — verify container and health endpoint
- `scripts/stop_inference.ps1` — stop the local container
- `scripts/start_model.ps1` — warm the configured model on the local box
- `scripts/test_inference.ps1` — run direct object detection against the local box
- `scripts/validate_workflow.ps1` — validate the workflow JSON against the local box
- `scripts/run_workflow.ps1` — run the workflow against an image and optionally save JSON

## Expected Environment

- `ROBOFLOW_API_KEY` — optional for starting a hosted/private model on the local box
- `MODEL_ID` — required for `start_model.ps1`, `test_inference.ps1`, and workflow runs
- `INFERENCE_PORT` — optional; defaults vary by helper script, use `9002` for the documented local demo flow

## Quick Start

```powershell
$env:INFERENCE_PORT = "9002"
$env:MODEL_ID = "your-project/1"

pwsh -NoProfile -File .\scripts\start_inference.ps1 -Port 9002
pwsh -NoProfile -File .\scripts\health_check.ps1
pwsh -NoProfile -File .\scripts\start_model.ps1
pwsh -NoProfile -File .\scripts\validate_workflow.ps1 -WorkflowPath .\workflow\asset_pipeline.json
pwsh -NoProfile -File .\scripts\run_workflow.ps1 -WorkflowPath .\workflow\asset_pipeline.json -ImagePath .\test_image.png -SaveJson
```

## Documentation

- `docs/roboflow/LOCAL_DEMO_CHEAT_SHEET.md` — fastest presentation path
- `docs/roboflow/AUTOMATION.md` — automation and playbook overview
- `docs/roboflow/BUILD_GUIDE.md` — longer build-and-demo guide

## Release Source

This bundle is generated from `public-release/roboflow-local-demo.manifest.json` in the monorepo and synced into the public `Aitherium/aither` repository by `.github/workflows/sync-alpha.yml`.

## Release Channels

- Standard source release via `.github/workflows/release-manager.yml`
	- Asset name: `roboflow-local-demo-vX.Y.Z.zip`
- Distribution release via `.github/workflows/distribution-release.yml`
	- Asset name: `roboflow-local-demo-dist-vX.Y.Z.zip`

In both cases, the zip is generated from the same manifest-driven bundle definition so the public payload stays consistent across release channels.