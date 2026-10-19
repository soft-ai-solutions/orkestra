---
feature: {{run_id}}
status: draft
size: {{size}}
created: {{date}}
---
# {{title}}

> NE ve NEDEN. Nasıl'ı plan.md anlatır. Onaydan sonra bu dosya DONDURULUR (hook ile zorlanır).

## Amaç ve değer
<Kullanıcı/ürün için hangi problem çözülüyor, kim faydalanıyor, başarı neye benzer — 2-4 cümle.>

## Yeniden üretim
<Yalnız hata düzeltmede, aksi halde bu bölümü sil: hatayı gösteren komut, gözlenen ve beklenen sonuç.>

## Kapsam
### Kapsam içi
- <madde>
### Kapsam dışı
- <bilerek yapılmayacaklar — scope creep'e karşı>

## Kullanıcı hikayeleri
- **US1 (P1)** — <rol> olarak <ihtiyaç> istiyorum, böylece <fayda>.
  Bağımsız test: <bu hikaye tek başına nasıl doğrulanır>

## Gereksinimler
- **FR-001**: Sistem <ölçülebilir davranış> MUST.
- **FR-002**: ...

## Başarı kriterleri
- **SC-001**: <ölçülebilir, teknoloji-bağımsız sonuç; ör. "boş form gönderiminde 3 alan için hata mesajı görünür">

## Kenar durum matrisi
| # | Durum | Girdi | Beklenen | Gereksinim |
|---|---|---|---|---|
| E1 | <boş/null/hatalı/eşzamanlı/çevrimdışı/yetkisiz...> | ... | ... | FR-00x |

## Sınırlar
- Her zaman: <AGENTS.md + proje değişmezleri; mevcut davranışı koru; minimal diff>
- Asla: <commit/push/deploy yok; istek dışı refactor/bağımlılık yok; proje karıştırma yok>

## Önerilen ek kapsam (zenginleştirme)
<Orkestranın önerdiği, kullanıcının KABUL ettiği/REDDETTİĞİ ekler. Kabul edilmeyen hiçbir şey kapsama girmez.>
- [kabul|ret] <öneri> — <neden değerli>

## Netleştirmeler
### Oturum {{date}}
- S: <soru> → C: <cevap>  (ya da: "Soru gerekmedi — gerekçe: ...")

## Varsayımlar ve açık riskler
- <varsayım> — <yanlışsa etkisi>
