"""evidence: human-facing messages (English + Turkish). Keys are `evidence.<name>`; both dicts must hold the same keys and {fields}.

The report's "machine evidence" block is rendered from these rows. The Turkish rows are byte-identical to what
earlier versions wrote, so a report from an older run still counts as current (see `evidence.report_has_current_block`,
which accepts the block in any supported language).
"""
from __future__ import annotations

from typing import Dict

EN: Dict[str, str] = {
    "evidence.title": "Machine evidence",
    "evidence.approval": "- Approval: **{decision}** · source {source} · {at} · plan digest `{hash}`",
    "evidence.approval_none": "- Approval: no record",
    "evidence.gate_header": "| Gate | Baseline | Last verify |",
    "evidence.regression": "Regression: {state}",
    "evidence.regression_none": "none",
    "evidence.regression_found": "YES / not verified",
    "evidence.pre_existing": " · broken from the start: {names}",
    "evidence.task_header": "| Task | Red (exit) | Green (exit) | Note |",
    "evidence.integrity": "Test integrity: {state}",
    "evidence.integrity_clean": "clean",
    "evidence.integrity_problem": "PROBLEM: {details}",
    "evidence.exceptions": "Approved exceptions: {paths}",
    "evidence.changed": "Changed files ({count}): {files}",
    "evidence.triage": "Review triage: {fix} fixed · {defer} deferred · {reject} rejected",
    "evidence.no_commit": "Commit/push: not done (changes are in the working tree)",
    "evidence.sc_quality": "Scorecard: first-pass review {rate} ({first}/{done} tasks) · review reopens {reopens} ({per} per task) · regressions caught {regressions}",
    "evidence.sc_flow": "Time to first red: {first_red} · deferred {deferred} · blocked {blocked}",
    "evidence.sc_red_mix": " · red reasons: {mix}",
    "evidence.sc_tokens": "Tokens: {total} (input {input}, output {output}) · cache read {cache} of input",
    "evidence.sc_phases": " · per phase: {phases}",
    "evidence.sc_cost": "Estimated cost: ${cost} (local estimate from token counts and configured prices)",
    "evidence.sc_cost_none": "Estimated cost: unknown (set scorecard.price_usd_per_mtok to estimate)",
    "evidence.sc_cost_warn": "warning: estimated cost ${cost} is above scorecard.cost_warn_usd (${limit})",
}

TR: Dict[str, str] = {
    "evidence.title": "Makine kanıtı",
    "evidence.approval": "- Onay: **{decision}** · kaynak {source} · {at} · plan özeti `{hash}`",
    "evidence.approval_none": "- Onay: kayıt yok",
    "evidence.gate_header": "| Gate | Baseline | Son verify |",
    "evidence.regression": "Regresyon: {state}",
    "evidence.regression_none": "yok",
    "evidence.regression_found": "VAR / doğrulanmadı",
    "evidence.pre_existing": " · baştan beri kırık: {names}",
    "evidence.task_header": "| Görev | Red (exit) | Green (exit) | Not |",
    "evidence.integrity": "Test bütünlüğü: {state}",
    "evidence.integrity_clean": "temiz",
    "evidence.integrity_problem": "SORUN: {details}",
    "evidence.exceptions": "Onaylı istisnalar: {paths}",
    "evidence.changed": "Değişen dosyalar ({count}): {files}",
    "evidence.triage": "Review triage: {fix} düzeltildi · {defer} ertelendi · {reject} reddedildi",
    "evidence.no_commit": "Commit/push: yapılmadı (değişiklikler çalışma ağacında)",
    "evidence.sc_quality": "Karne: ilk turda geçen review {rate} ({first}/{done} görev) · review yeniden açma {reopens} (görev başına {per}) · yakalanan regresyon {regressions}",
    "evidence.sc_flow": "İlk kırmızıya kadar süre: {first_red} · ertelenen {deferred} · bloke {blocked}",
    "evidence.sc_red_mix": " · kırmızı nedenleri: {mix}",
    "evidence.sc_tokens": "Token: {total} (girdi {input}, çıktı {output}) · girdinin {cache} kadarı önbellekten",
    "evidence.sc_phases": " · aşama başına: {phases}",
    "evidence.sc_cost": "Tahmini maliyet: ${cost} (token sayıları ve ayarlı fiyatlardan yerel tahmin)",
    "evidence.sc_cost_none": "Tahmini maliyet: bilinmiyor (tahmin için scorecard.price_usd_per_mtok ayarlayın)",
    "evidence.sc_cost_warn": "uyarı: tahmini maliyet ${cost}, scorecard.cost_warn_usd sınırını (${limit}) aşıyor",
}
