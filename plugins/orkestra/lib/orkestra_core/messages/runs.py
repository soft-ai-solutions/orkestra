"""runs: human-facing messages (English + Turkish). Keys are `runs.<name>`; both dicts must hold the same keys and {fields}."""
from __future__ import annotations

from typing import Dict

EN: Dict[str, str] = {
    # memory/FEATURES.md, written only when the file has no table yet (the template normally provides it)
    "runs.features_header": ("# Features\n\nOne line per delivered orkestra run (newest last). Details live in "
                             "`.orkestra/features/<id>/`.\n\n| id | title | date | capabilities | summary |\n"
                             "|---|---|---|---|---|\n"),
    # `orkestra gate baseline|verify` table header
    "runs.gate_table_header": "| gate | baseline | now | time | command |",
    # `gate verify` footers: gates not executed because they already failed at baseline / carried from an earlier verify
    "runs.gates_unprotected": "gates NOT protecting this run (failed at baseline, not re-run; `gate verify --all` runs them): {names}",
    "runs.gates_carried": "gates carried forward (no file in their paths changed since their last PASS): {names}",
    # a gate that hit its timeout: hint appended to the TIMEOUT line of the log and the failure tail
    "runs.gate_timeout_hint": "(raise timeout in .orkestra/config.json (now {seconds}s) or set enabled:false)",
    # gates that failed because the tool or its dependencies are missing, kept apart from real failures
    "runs.gates_env_error": "{count} gates could not run (missing tool or deps): {names}. Install the tool or dependencies, or set enabled:false in .orkestra/config.json.",
    "runs.gates_none_ran": "WARNING: no gate could run, so nothing was checked. This is not a pass.",
    "runs.gates_env_waiver_needed": "gates could not run (missing tool or deps): {names}. Fix the environment, or record why in gates-waiver.md in the feature folder.",
    # `orkestra start` in a folder without git
    "runs.no_git_rollback": "No git here: Orkestra can only restore files edited through its hooks. Run git init for full rollback.",
    # activity row when a run changes status: who asked (a chat id, or cli), from, to
    "runs.status_row": "{by}: status {src} → {dst}",
    "runs.plan_bugfix_tdd": "request type is {kind}: at least one task must require TDD (repro-first). Add a task with `- TDD: required` whose test reproduces the symptom and fails before the fix.",
    "runs.plan_bugfix_symptom": "{task}: request type is {kind}, so the first task must reference the symptom requirement. Name {fr} (or the FR that states the symptom) in its acceptance line.",
    "runs.size_escalated": "size raised {old} → {new} at plan time: {why}",
    "runs.size_escalated_why_risky": "the plan touches security or data paths ({files})",
    "runs.size_escalated_why_tasks": "the plan has {count} tasks (more than {limit})",
    "runs.size_escalated_why_files": "the plan touches {count} files (more than {limit})",
}

TR: Dict[str, str] = {
    "runs.features_header": ("# Features\n\nTeslim edilen her orkestra run'ı için bir satır (en yenisi en sonda). "
                             "Ayrıntılar `.orkestra/features/<id>/` altında.\n\n"
                             "| id | başlık | tarih | yetenekler | özet |\n|---|---|---|---|---|\n"),
    "runs.gate_table_header": "| gate | baseline | now | süre | komut |",
    "runs.gates_unprotected": "Bu run'ı KORUMAYAN gate'ler (baseline'da başarısız, yeniden çalıştırılmadı; `gate verify --all` çalıştırır): {names}",
    "runs.gates_carried": "Önceki sonuçtan taşınan gate'ler (son PASS'ten beri paths içinde dosya değişmedi): {names}",
    "runs.gate_timeout_hint": "(.orkestra/config.json içinde timeout değerini artırın (şimdi {seconds}s) ya da enabled:false yapın)",
    "runs.gates_env_error": "{count} gate çalışamadı (araç ya da bağımlılık eksik): {names}. Aracı ya da bağımlılıkları kurun, veya .orkestra/config.json içinde enabled:false yapın.",
    "runs.gates_none_ran": "UYARI: hiçbir gate çalışamadı, yani hiçbir şey denetlenmedi. Bu bir geçiş değildir.",
    "runs.gates_env_waiver_needed": "gate'ler çalışamadı (araç ya da bağımlılık eksik): {names}. Ortamı düzeltin ya da nedenini özellik klasöründeki gates-waiver.md dosyasına yazın.",
    "runs.no_git_rollback": "Burada git yok: Orkestra yalnızca kendi hook'larıyla düzenlenen dosyaları geri yükleyebilir. Tam geri alma için git init çalıştırın.",
    "runs.status_row": "{by}: durum {src} → {dst}",
    "runs.plan_bugfix_tdd": "istek türü {kind}: en az bir görev TDD gerektirmeli (önce yeniden üretim). Testi belirtiyi yeniden üreten ve düzeltmeden önce başarısız olan, `- TDD: required` satırlı bir görev ekleyin.",
    "runs.plan_bugfix_symptom": "{task}: istek türü {kind}, bu yüzden ilk görev belirti gereksinimine başvurmalı. Kabul satırında {fr} (ya da belirtiyi anlatan FR) adını yazın.",
    "runs.size_escalated": "boyut plan aşamasında yükseltildi {old} → {new}: {why}",
    "runs.size_escalated_why_risky": "plan güvenlik veya veri yollarına dokunuyor ({files})",
    "runs.size_escalated_why_tasks": "planda {count} görev var ({limit} sınırından fazla)",
    "runs.size_escalated_why_files": "plan {count} dosyaya dokunuyor ({limit} sınırından fazla)",
}
