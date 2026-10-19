# Teslim raporu — {{title}}

- Run: `{{run_id}}` · Proje: {{project}} · Tarih: {{date}}
- Durum: TAMAMLANDI | KISMİ (neden) — dürüst olun

## Ne yapıldı ve ürüne değeri
<2-5 cümle: kullanıcı açısından ne değişti, hangi problemi çözdü.>

## Kanıt
Makine kanıtı bloğunu `orkestra report evidence --write` ekler (onay, gate baseline→verify, görev başına red/green,
test bütünlüğü, değişen dosyalar, triage). Bu bloğu elle düzenleme; buradaki her sayı oradan gelmeli.

## Plandan sapmalar
<onaylı plan ile final plan farkı; yoksa "Yok">

## Yapılmayanlar / ertelenenler
<defer edilen review maddeleri, kapsam dışı bırakılanlar; yoksa "Yok">

## Yaptığım varsayımlar (otopilot)
<spec.md ve plan değişiklik günlüğünden en önemli 3 varsayım, her biri nasıl geri alınır; yoksa "Yok".>

## Nasıl denersin
<adım adım: komut / ekran / URL>

## Riskler ve takip önerileri
<...>

## Commit önerisi
Hiçbir şey commit'lenmedi — değişiklikler çalışma ağacında. Önerilen mesaj:
```
<type>(<scope>): <özet>

<gövde>
```
