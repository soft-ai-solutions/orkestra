# Orkestra documentation / Orkestra belgeleri

English first, Türkçe aşağıda. The documents for users exist in both languages; the contributor and project documents
(CONTRIBUTING, SECURITY, AGENTS, CHANGELOG) are in English only, and the tables below say so.

## English

| I want to | Read |
| --- | --- |
| Know what Orkestra is and run it in a few commands | [README.md](../README.md) |
| Set up a new machine (macOS, Linux, Windows) | [ONBOARDING.md](../ONBOARDING.md) |
| See the architecture and the flow as diagrams | [architecture.html](architecture.html) (open it in a browser) |
| Read the one-page reference (commands, files, guards, monitoring, scope) | [ORKESTRA.md](../ORKESTRA.md) |
| Connect Telegram, n8n or OpenClaw | the Bridge section of the [README](../README.md#bridge-telegram-n8n-and-openclaw) and the [OpenClaw contract](../integrations/openclaw/CONTRACT.md) |
| Switch the language | the Language section of the [README](../README.md#language) |
| Contribute code, text or a translation | [CONTRIBUTING.md](../CONTRIBUTING.md) (English only) |
| Report a vulnerability | [SECURITY.md](../SECURITY.md) (English only; you may write in Turkish) |
| See what changed | [CHANGELOG.md](../CHANGELOG.md) (English from the Unreleased entry on; older entries are Turkish) |
| Edit this repository as an agent | [AGENTS.md](../AGENTS.md) (English only) |
| Read the decisions behind distribution, safety defaults, cost and privacy, and what is not done | [decisions.md](decisions.md) |

## Türkçe

| Şunu yapmak istiyorum | Oku |
| --- | --- |
| Orkestra'nın ne olduğunu öğrenmek ve birkaç komutta çalıştırmak | [README.tr.md](../README.tr.md) |
| Yeni bir makineyi kurmak (macOS, Linux, Windows) | [ONBOARDING.tr.md](../ONBOARDING.tr.md) |
| Mimariyi ve akışı şemalarla görmek | [orkestra-mimari.html](orkestra-mimari.html) (tarayıcıda aç) |
| Tek sayfalık başvuruyu okumak (komutlar, dosyalar, guard'lar, izleme, kapsam) | [ORKESTRA.tr.md](../ORKESTRA.tr.md) |
| Telegram, n8n ya da OpenClaw bağlamak | [README](../README.tr.md#bridge-telegram-n8n-ve-openclaw) içindeki Bridge bölümü ve [OpenClaw sözleşmesi](../integrations/openclaw/CONTRACT.tr.md) |
| Dili değiştirmek | [README](../README.tr.md#dil) içindeki Dil bölümü |
| Kod, metin ya da çeviri ile katkı vermek | [CONTRIBUTING.md](../CONTRIBUTING.md) (İngilizce) |
| Güvenlik açığı bildirmek | [SECURITY.md](../SECURITY.md) (İngilizce; Türkçe yazabilirsin) |
| Neyin değiştiğini görmek | [CHANGELOG.md](../CHANGELOG.md) |
| Dağıtım, güvenlik varsayılanları, maliyet ve gizlilik kararlarını ve yapılmayanları okumak | [decisions.tr.md](decisions.tr.md) |

## About these files

- `architecture.html` (English) and `orkestra-mimari.html` (Turkish) are static pages with the same diagrams, tables
  and facts, and a language switch link at the top of each. The Turkish page has a single copy, in this folder.
- The pages describe the code at a point in time and say so (they carry the version and a date; the run statistics are
  dated 4-5 October 2026). When the code changes, update both languages together. `tests/test_docs_drift.py` checks
  that the two pages keep the same structure.
- Doc tests: `cd plugins/orkestra/tests && python3 -m unittest test_docs_drift test_public -q`.
