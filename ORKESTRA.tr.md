# Orkestra: özet (sürüm 1.6.0, 19 Ekim 2026)

> English: [ORKESTRA.md](ORKESTRA.md) · Bu dosya İngilizce kanonik başvurunun Türkçe karşılığıdır; içerik aynıdır.

Bu dosya Orkestra'nın tamamını tek yerde özetler. Kullanım ayrıntısı `README.md`'de, sürüm geçmişi `CHANGELOG.md`'de,
görsel anlatım `docs/orkestra-mimari.html`'de, katkı kuralları `CONTRIBUTING.md`'dedir.

## Ne olduğu

Orkestra, Claude Code, Cursor ve GitHub Copilot (ve Pi) için bir eklentidir. Bir geliştirme isteğini on fazdan geçirir: giriş, keşif, netleştirme, plan,
**senin onayın**, uygulama, doğrulama, inceleme, teslim, bitti. Kurallar modelin iyi niyetine bırakılmaz: faz sırasını
ve kanıtı bir komut satırı aracı (`orkestra`) tutar, yasakları host hook'ları her araç çağrısında denetler.

Planı sen onaylamadan kaynak koda yazılamaz. Her görev önce başarısız, sonra geçen test kanıtıyla kapanır. Run başında
geçen her kontrol (test, lint, tip, build) sonda da geçmek zorundadır. Commit, push, deploy ve mağaza yüklemesi akışın
parçası değildir; yalnız o mesajda açıkça istersen yapılır.

## Dil

Orkestra İngilizce ve Türkçe hizmet verir; varsayılan İngilizcedir. İnsanın okuduğu metnin dili (panolar, `watch`,
etkinlik satırları, bildirimler, run belgeleri, kurulum ve bridge cevapları, `--help`) şu sırayla belirlenir, ilk bulunan
kazanır:

1. `ORKESTRA_LANG` ortam değişkeni (`en`, `tr` veya `auto`);
2. `~/.orkestra/config.json` içindeki `lang` anahtarı (`en`, `tr` veya `auto`; varsayılan `auto`);
3. işletim sisteminin yerel ayarı (`LC_ALL`, `LC_MESSAGES`, `LANG`, `LANGUAGE`; `tr…` yerel ayarı Türkçeyi seçer; Windows'ta bunların hiçbiri yoksa kullanıcı yerel ayarı);
4. İngilizce.

`orkestra lang show` kullanılan dili ve nereden geldiğini basar; `orkestra lang en|tr|auto` seçimi kaydeder
(yukarıdaki `lang` anahtarı); `--lang en|tr` yalnız o komutun dilini değiştirir.

İki dilde de değişmeyenler:

- Modelin okuduğu metin (skill'ler, ajanlar, playbook'lar, `orkestra next` ipuçları, guard mesajları) İngilizcedir.
- Senin yazdığın iki dilde de anlaşılır: `commit this` / `commitle`, `approve` / `onay` gibi.
- `request.md`, `spec.md`, `plan.md` ve `report.md` bölüm başlıkları iki dilde de vardır
  (`lib/orkestra_core/messages/protocol.py`). Her ayrıştırıcı iki yazımı da kabul eder; bir dilde başlayan run öbür dilde
  sürdürülebilir, eski Türkçe run'lar çalışmaya devam eder.
- Belge şablonları `plugins/orkestra/templates/en/` ve `plugins/orkestra/templates/tr/` altındadır;
  `orkestra template <ad>` etkin dilin şablonunu basar.

## Mimari

Dört parça var; hepsi `plugins/orkestra/` altında (Claude Code, `orkestra setup`'ın önbelleğe aldığı kopyayı çalıştırır; Cursor ve Copilot checkout'u okur):

| Parça | Yer | İş |
|---|---|---|
| Skill'ler ve playbook'lar | `skills/` | Ana oturuma (orkestratör) her fazda ne yapacağını söyleyen metinler. Faz başına bir playbook; isteğe bağlı kontrol listeleri yalnız değişen dosyalar eşleşince yüklenir. |
| Ajanlar | `agents/` | Sekiz alt-ajan: scout, researcher, architect, plan-critic, implementer, reviewer, adversary, verifier. Yalnız implementer kaynak koda yazabilir. |
| CLI | `bin/orkestra` → `lib/orkestra_core/` | Durum makinesi (`runs.py`, `state.py`), kapılar (`gates.py`), TDD ve kanıt (`evidence.py`), geri alma yedekleri (`snapshot.py`), sıradaki adım ve süre (`tempo.py`), izleme (`monitor.py`, `usage.py`), dış tarama (`watchdog.py`). |
| Hook'lar | `hooks/hooks.json` → `hooks/dispatch.py` | On olayın tek giriş noktası: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStop ve etkinlik akışını doğru tutan dördü (başarısız araç çağrısı, izin istemi, API hatası, oturum sonu): PostToolUseFailure, Notification, StopFailure, SessionEnd. `dispatch.py` bir saplamadır; kod `lib/orkestra_core/hookmain.py` içindedir ve hiçbir şey yapmayacağı kanıtlanabilen olaylar o yüklenmeden çıkar. Guard kararları `guards.py`'de; kabuk komutunu `shellparse.py`, `orkestra …` satırını CLI'nin kendi ayrıştırıcısı (`cliparse.py`) okur. Hook içindeki bir hata oturumu kilitlemez (izin verir, `~/.orkestra/logs/hooks.log`'a yazar). Bütün bir hook için tek süre sınırı vardır: 6 sn CPU ya da 9 sn duvar saati; sınıra varan PreToolUse çağrısı reddedilir (`complex` guard'ı, makine yalnız yavaşsa `busy`), diğer olaylar sessizce biter. Yeni hook girdileri yalnız yeni açılan oturumlarda etkili olur. |

`bridge/` ayrı bir süreçtir: Telegram, n8n ya da OpenClaw'dan gelen isteği başsız bir oturuma (Claude Code,
Cursor ya da GitHub Copilot) çevirir. Aynı hook'lar orada da çalışır.

### Motorlar (Cursor 1.5.0 ile geldi)

Bir run'ı Claude Code (varsayılan), Cursor ya da GitHub Copilot sürer; seçim istek başınadır (`orkestra run --engine cursor`,
`/yap cursor <proje> <istek>` (İngilizcesi `/run`), bridge'de `"engine": "cursor"`, Cursor sohbetinde `/orkestra-run`). Durum makinesi,
kapılar, kanıt ve guard'lar tektir. Motora özgü olan her şey üç dosyadadır:

| Dosya | İş |
|---|---|
| `lib/orkestra_core/engines.py` | Motor adları, oturum anahtarı (`cursor-<sohbet>`), komut satırları, ilk istem, metin çevirisi (`orkestra:reviewer` → `orkestra-reviewer`, yalnız Claude'da doğru cümlelerin Cursor karşılığı). |
| `hooks/cursor_hook.py` | Cursor olaylarını `dispatch.py`'nin işleyicilerine çevirir: alt-sohbet → ana oturum eşlemesi, istem/tur izleme, kabuk dizini izleme (`shellcwd.py`), kendisini ajanın başlattığını tanıma. |
| `lib/orkestra_core/cursorkit.py` | Cursor skill/alt-ajan dosyalarını üretir (`plugins/orkestra/cursor/`), `~/.cursor`'a kurar, `hooks.json`'ı birleştirir. |

Cursor'da ölçülen ve tasarımı belirleyen davranışlar: her alt-ajan ayrı bir sohbettir ve olaylarında türü yoktur;
baskı kipinde (`agent -p`) istem ve durma olayı gelmez; kabuk dizinini çağrılar arasında korur ama olayda bildirmez;
sıfırdan farklı çıkan komut `postToolUseFailure` ile biter; alt-ajan bitişi için olay yoktur. Ayrıntı ve örnek yükler:
`CHANGELOG.md` (1.5.0), `tests/fixtures/cursor/`.

## Komutlar

Sohbet içinde: `/orkestra:run <istek>`, `/orkestra:status`, `/orkestra:resume`, `/orkestra:onboard`,
`/orkestra:rollback`, `/orkestra:ship` (commit + push; yalnız sen çağırırsın). Cursor'da aynıları tire ile:
`/orkestra-run` … `/orkestra-ship`.

Terminalde (`python3 $ORKESTRA_ROOT/plugins/orkestra/bin/orkestra …` veya `./plugins/orkestra/bin/orkestra`; kurucu `orkestra`yı PATH'ine koyduktan sonra yalnız `orkestra …`):

| Komut | İş |
|---|---|
| `watch` · `dashboard --open` | Canlı görünüm (terminal · http://127.0.0.1:47616). Sayfa riskli ajan işlemlerini gösterir ve bir run'ı duraklatabilir ya da iptal edebilir (`--read-only` kontrolleri gizler). |
| Bridge chat UI | http://127.0.0.1:47615/ · proje/Claude/Cursor/Copilot seç; faz haritası, ajan ağı, görevler ve son hook hareketlerini izle. |
| `status -p <proje>` · `context` | Run'ın fazı, görevleri, kanıt eksikleri. |
| `next -p <proje>` | Sıradaki tek adım. Aynı durumda hep aynı cevap. |
| `tempo -p <proje> [--run <id>]` | Süre dökümü: fazlar, görev başına süre, inceleme retleri, ölü bekleme payı. |
| `watchdog [--dry-run\|--quiet\|--launchd]` | Açık her run'ın oturum dışından tek taraması; bildirir. |
| `watchdog --loop\|--daemon\|--status` | Hızlı izleyici: `--loop` ön planda çalıştırır, `--daemon` ayrık başlatır (POSIX), `--status` çalışıp çalışmadığını ve neyi işaretlediğini gösterir (makine görünümü için `--json`). İzleme bölümüne bak. |
| `wait [--max <sn>] -p <proje>` | Başsız run'lar: alt-ajan raporları için süreç içinde (token harcamadan) bekler, sonra sıradaki adımı yazar. Varsayılan tavan 540 sn. Etkileşimli oturum bloke olmaz. |
| `gate verify [--resume\|--all] -p <proje>` | Regresyon yok denetimi. `--resume` yarım kalan verify'a devam eder; `--all` her kapıyı baştan çalıştırır. |
| `brief [--review] <görev>` | Tek görevin özeti; `--review` ile reviewer paketi (kabul ölçütü, red ve green kuyrukları, rapor, dosyalarının diff'i). |
| `agents [clear] -p <proje>` | Arka plandaki alt-ajan kayıtları. |
| `doctor` | Ortam ve hook öz-testi. |
| `lang [show\|en\|tr\|auto]` | İnsana giden metnin dili ve nereden geldiği; `en`, `tr` ve `auto` seçimi kaydeder. Herhangi bir komutta `--lang` yalnız o çağrıyı değiştirir. |
| `await\|block\|unblock\|pause\|abort -p <proje> --reason "<metin>"` | Gerekçe düz kelimelerle de verilebilir. `orkestra --version` sürümü yazar. |
| `run [--engine claude\|cursor\|copilot] -p <proje> "<istek>"` | Seçilen motorun kendi arayüzünü projede açıp run'ı başlatır. |
| `cursor install \| uninstall \| status \| build` | Cursor tarafı: skill, alt-ajan ve hook kurulumu (senin `hooks.json` girdilerin korunur). |
| `copilot install \| uninstall \| status` | Copilot tarafı: hook kurulumu (senin `hooks.json` girdilerin korunur). |

Geri kalan alt komutları (`phase`, `task`, `tdd`, `gate`, `evidence`, `plan check`, `memory` …) orkestratör ve ajanlar
kullanır; elle çağırman gerekmez. Seçenekler tam yazılır; kısaltma (`--no` gibi) kabul edilmez.

## Dosyalar

Her projede `.orkestra/` oluşur:

- `features/NNN-slug/` — insanın okuduğu belgeler: `request.md`, `context.md`, `spec.md`, `plan.md`, `ledger.md`
  (her faz geçişi ve kanıt satırı), `tasks/`, `reviews/`, `verification.md`, `report.md`. Git'e girebilir.
- `runs/NNN-slug/` — makine durumu: `state.json`, `approval.json`, `agents.json`, `activity.jsonl`, kanıt logları,
  dosya yedekleri. Git'e girmez; yalnız CLI ve hook'lar yazar.
- `memory/` — proje hafızası: `PROJECT.md`, `FEATURES.md`, `specs/`, `DECISIONS.md`, `LEARNINGS.md`.
- `config.json` — projenin kapı komutları. `current` — açık run'a kısa yol.

Makinede `~/.orkestra/`: `config.json` (guard anahtarları, `watchdog` eşikleri, `lang`), oturum–run bağları, loglar, bridge
ayarı. Model bu klasöre yazamaz.

## Guard'lar

Her Claude Code oturumunda: **git** (commit/push/branch yalnız o mesajda istenirse; alt-ajan raporu izin taşımaz),
**secrets** (`.env*`, anahtar dosyaları okunamaz ve yazılamaz), **store** (EAS/Expo yüklemeleri), **home**
(`~/.orkestra`), **rollback**, **complex** (denetlenemeyecek kadar büyük komut).

Yalnız run'a bağlı oturumda: **phase** (onaydan önce kaynak koda yazma yok), **isolation** (proje dışına yazma yok),
**deps** (onaysız paket yok), **deploy**, **destructive** (özyinelemeli silme yalnız ayrılmış bir geçici alt klasörde),
**roles** (salt-okur ajanlar yalnız rapor yazar), **internals** (makine
durumuna elle ya da betikle yazma yok; onay cevabı gömülemez; ayrıca başka bir sohbetin yürüttüğü run için
`orkestra pause|abort|resume|unblock|block|await`, bunu yalnız senin kendi mesajın kaldırır), **wait** (alt-ajan için uyuma ve yoklama yok),
**dialogs** (ekranda sistem penceresi açan komut yok), **Stop** (run aktifken tur bitirilirse sıradaki adımla devam
ettirilir, ilerleme durumu başına en çok iki, 30 dakikada en çok altı kez; alt-ajan çalışırken bitmesine izin verilir).

Bir run guard'ının her reddi çıkış yolunu söyleyen bir `unblock:` satırıyla biter: run'ı yürüten sohbet `orkestra pause`
ya da `orkestra abort` yapabilir, diğer her ajan sana söyler. Reddedilen çağrı guard, araç, hedef ve aktöre göre sayılır:
2. aynı ret bir not ekler, 3.'sü "denied Nx: do not retry; <alternatif>" olur (varsa tam bir `orkestra ...` yeniden
yazımıyla), 10 dakikada 8.'si run'ı bloke eder ve bir `loop` satırı yazar. Kararın kendisi hiç değişmez. İki ret daha
hook'un kendisinden gelir: `busy` (çağrı yoğun bir makinede 9 sn içinde denetlenemedi; aynı çağrıyı bir kez yeniden dene)
ve `approval` (otopilot run'da soru).

Bilinçli istisna: `/tmp/<klasör>` (ve `$TMPDIR` altı) izolasyondan muaftır, çünkü ajanların deneme ve kopya için bir
yere ihtiyacı var. Geçici kökün kendisi (`/tmp/*`) muaf değildir.

Dokuz guard `~/.orkestra/config.json` → `guards` altından kapatılabilir. `guards.test_syntax` ters yönde bir
seçenektir ve kapalı gelir: açılırsa `[[ a > b ]]` ve `(( n > 100 ))` yönlendirme değil karşılaştırma olarak okunur.
bash'e karşı doğrulandı, zsh'e karşı doğrulanmadı; o yüzden kapalı.

## İzleme

`watch` ve `dashboard` her run için fazı, o fazda geçen süreyi, görev ve kanıt ilerlemesini, kapı sonuçlarını, token
kullanımını, son araç çağrılarını ve **duraklama sınıfını** gösterir:

1. seni bekliyor (soru, onay, bloke, duraklatıldı, tur bitti) · 2. ölü bekleme (duran bir alt-ajanın raporu 60 sn
gecikti, kayıt defteri bunu söylüyor ve tüm run o zamandan beri sessiz) · 3. asılı komut (`hung_command_seconds`, 900 sn,
ya da `gate_command_seconds`, 1800 sn boyunca açık; başladığından beri hiçbir ajan satır yazmadı) · 4. sessiz oturum
(10 dakikadır kayıt yok) · 5. döngü (aynı ret ya da aynı komut 3 kez, üçüncü inceleme turu).

Her sınıf satırlar, kayıt dosyaları ve saatler üzerinde düz bir koşuldur; hiçbiri "model sessiz kaldı" değildir, çünkü
düşünen bir model durmuş değildir. Yanlış bir durma ilanı geç ilandan pahalıdır.

`watchdog` aynı sınıflandırmayı oturumun dışından yapar ve yeni olayda bir kez masaüstü bildirimi (macOS, ya da `notify-send` olan Linux) gönderir. Bridge
çalışıyorsa dakikada bir kendisi koşar; çalışmıyorsa `orkestra watchdog --launchd` bir LaunchAgent dosyası yazar.
Otomatik devam yalnız başsız başlatılmış run'lar içindir (en çok 3 kez, aynı oturumda, o oturumun süreci yaşarken
asla). Etkileşimli pencereden yürüttüğün run kendiliğinden sürdürülmez.

### Hızlı izleyici

`orkestra watchdog --loop|--daemon|--status` (`lib/orkestra_core/fastwatch.py`). Makine başına bir tane: kilitli bir pid
dosyası (`~/.orkestra/watchdog.pid`; kilit süreçle birlikte ölür, `kill -9` sonrası kalan dosya devralınır). Açık her
run'ı 5 sn'de bir kontrol eder (durma ilan edilmek üzereyken 1 sn, açık run yokken 15 sn), bir dosyayı yalnız boyutu ya
da mtime'ı değişince yeniden okur ve token harcamaz. Hiçbiri çalışmıyorsa SessionStart başlatır (POSIX;
`watchdog.autostart`, `ORKESTRA_NO_DAEMON=1` kapatır); bridge onu iş parçacığı olarak barındırır; açık run ve canlı
oturum kalmayınca `watchdog.daemon_idle_minutes` sonra çıkar.

| Koşul | Doğru olduğu an | Bekleme süresi |
|---|---|---|
| ret döngüsü | aynı ret 120 sn'de 3 kez (600 sn'de 8 kez: run bloke edilmeli) | yok |
| gidip gelme | 10 dk'da 3 pause/resume durum değişikliği | yok |
| duraklatılmış ama çalışıyor | ajanları son 60 sn'de araç çalıştıran duraklatılmış run | 5 sn |
| rapor gecikti | duran alt-ajanın raporu bekleniyor, devir yok, ana oturum eylemi yok, tüm run sessiz | 60 sn |
| boşta aktif | otopilot run'ın turu iş kalmışken bitti ve durum ilerlemedi | 20 sn |
| izin bekliyor | host'un gösterdiği izin istemini kimse cevaplamadı | 30 sn |
| asılı komut | sınırını aşmış açık komut; başladığından beri hiçbir ajan satır yazmadı ve süreci CPU göstermiyor | 15 ya da 30 dk |
| API hatası | model ya da API hatası turu bitirdi ve sonra hiçbir şey olmadı | yeniden deneme merdiveni |
| ölü oturum | bağlı her oturumun host süreci gitti | yok |
| hook bozuk | `hooks.log` içinde 60 sn'de 3 hook hatası | yok |

Yaptığı: olay başına bir bildirim (sonra 30 dk, 2 sa, 8 sa ve bir gün sonra, toplamda en çok 5); ve yalnız kanıtlanabilir
biçimde bayat olanı onarır: olmayan ya da kapanmış run'a bağlama, aynı ajanın sonraki satırının geçtiği açık komut (dürüst
bir sentetik `end` satırı), gitmiş ajanların alt-ajan kayıtları, gidip gelme fırtınası için bir ledger satırı. Başsız
run için oturumu sınırlı `claude -p --resume` ile sürdürür (run başına 3, 10, 60 ve 300 sn sonra; oturumun süreci ya da bir
bridge işi canlıyken asla). Yapamadığı: Windows'ta çalışmak, bir istemi onaylamak ya da soruyu cevaplamak, kendi
başlatmadığı süreci öldürmek, CPU örneği olmadan yavaş komutu takılmış komuttan ayırmak. Üst üste 3 başarısız tikten sonra
onarmayı bırakır, yalnız bildirir. Yaptığı her şey `~/.orkestra/logs/watchdog.log` içindedir (1 MB'ta döner).

## Süre ölçümü ve tempo

4–5 Ekim'de 1.2.1 ile koşan beş run'da ölçülenler (`orkestra tempo`):

| Run | Süre | Görev | İnceleme reddi | Ölü bekleme payı |
|---|---|---|---|---|
| game-app · kalan işler | 12 sa 37 dk | 28 | 17 | %29 |
| deals-app · derin analiz | 6 sa 57 dk | 57 | 67 | %19 |
| mobile-app 001 | 5 sa 55 dk | 27 | 13 | %34 |
| mobile-app 002 | 5 sa 58 dk | 28 | 19 | %26 |
| mobile-app 003 | 11 sa 2 dk (5 sa 18 dk'sı onay beklemesi) | 62 | 0 | %12 |

İki büyük kayıp vardı. Birincisi ölü bekleme: Claude Code alt-ajanı arka planda çalıştırıp raporu yeni mesajla
getirirken Stop hook'u turun bitmesini engelliyor, model de `sleep` döngüsüyle bekliyordu. 1.4.0'da ajan çalışırken
tur bitebilir, bekleme komutları reddedilir. İkincisi gereksiz inceleme turu: 116 reddin 35'i yalnız küçük notlar
içindi. Artık PASS alan görev yeniden açılmaz, notlar final incelemeye taşınır, üçüncü FAIL sana sorulur.

Bu değişikliklerin süreye etkisi henüz ölçülmedi. 1.4.0 ile koşan ilk run'dan sonra `orkestra tempo` aynı tabloyu verir;
karşılaştırma oradan yapılır.

Kesintisiz devam: yarıda kalan run için ayrı bir "nerede kaldım" kaydı yoktur. `orkestra next` cevabı state, kanıt ve
inceleme dosyalarından hesaplar; oturum kapansa, bağlam sıkışsa ya da yeni pencere açsan da aynı alt-adımı bulur.
`/orkestra:resume` bunu kullanır.

## Güvenilirlik: tavanlar, kilitler ve kapılar

Hiçbir ajan bir diğerini bloke etmez, hiçbir şey sınırsız döngüye girmez ve beklemek token harcamaz.

**Kontrol düzlemi.** Bir sohbet bir run'a yalnız ana sohbetin kendisi `orkestra start|resume` çalıştırdığında bağlanır (bağ
çözme `pause`, `abort` ya da `phase next` ile gelir), yazdırılan işaretin `home=` değeri bu hook'un `ORKESTRA_HOME`'u olur
ve run vardır, açıktır ve mutlak proje yolu vardır. Alt-ajanlar, bir logun `cat` çıktısı ve CLI'nin sandbox kopyaları hiçbir
şeye bağlanmaz. Canlı bir bağlama bir ledger satırı ve bir uyarıyla korunur; sahte olan kendiliğinden iyileşir; nötr bir
mesaj bağı asla çözmez. Başka bir sohbetin yürüttüğü run için `pause`, `abort`, `resume`, `unblock`, `block` ve `await`
kullanıcının mesajı istemedikçe reddedilir (sürücü `watchdog.lease_seconds` boyunca canlıdır). Hiçbir şeyi değiştirmeyen
çağrı (duraklatılmış run'ı duraklatmak) no-op'tur ve satır yazmaz. Bir `status` etkinlik satırı kimin duraklattığını ya da
sürdürdüğünü gösterir.

**Tavanlar.**

| Ne | Tavan | Sonra |
|---|---|---|
| aynı retler | 2. not, 3. "do not retry", 10 dk'da 8. | run bloke edilir, bir `loop` satırı yazılır |
| Stop dürtmeleri | ilerleme durumu başına 2, run başına 30 dk'da 6 | tur biter; otopilot dürtmeleri modeli soru sormaya göndermez |
| bir görevin inceleme turları | 3 | manuel: kullanıcı karar verir; otopilot: görev bloke edilir ve `--force` kaldıramaz (devamı `task defer` ya da `block`) |
| aynı görevlendirmeler | yeni kanıt olmadan `tempo.max_dispatches` (3) | `orkestra next` `escalate` cevabı verir |
| alt-ajan kayıt defteri | `watchdog.agent_silent_seconds` (150 sn) yaşam belirtisi yok | ajan kayıp olarak listelenir |
| bridge turu | `bridge.turn_timeout_seconds` (7200 sn) duvar saati, `bridge.turn_idle_seconds` (1800 sn) run hareketi yok | tur kesilir (SIGINT, süreç grubu 20 sn sonra) ve run sürdürülür; açık komutu olan tur asla boşta sayılmaz |
| başsız harcama | `headless.max_budget_usd` (0 = kapalı) | `claude -p --max-budget-usd` olarak iletilir |

**Kilitler ve süre sınırları.** Bir kilit hook'ta 2 sn beklenir (sonra hook kilitsiz devam eder ve bir satır kaydeder; durum
işlemi tek satırlık bir hata verir), CLI'de 10 sn; hata tutan sürecin pid'sini, komutunu ve yaşını söyler.
`ORKESTRA_LOCK_TIMEOUT` ikisini de değiştirir. Yavaş işler (git baseline'ları, test dosyası taraması, kullanım toplamları)
kilit alınmadan önce çalışır; her git çağrısı süre kutusundadır ve `GIT_OPTIONAL_LOCKS=0` ile çalışır. Hook süre sınırı
Mimari bölümündedir.

**Bekleme.** Başsız run'da `orkestra wait [--max N]` süreç içinde 2 sn'de bir bakar ve şunlardan ilkinde döner: çalışan
alt-ajan yok, yeni bir rapor beklenir, bir devir geldi, bir review ya da görev dosyası değişti, run `active` durumundan
çıktı ya da tavan (varsayılan 540 sn, bir Bash çağrısının çalışabileceği 600 sn'nin altında). Karar hâlâ çalışıyor diyorsa
yeniden çalıştır.

**Kapılar.** Her kapı kendi süreç grubunda, `evidence/` altında akan bir logla çalışır; zaman aşımı (`timeout`, varsayılan
900 sn) ya da boşta kalma (`idle_timeout`, ayarlanmadıkça kapalı) tüm grubu durdurur (SIGTERM, 3 sn sonra SIGKILL).
`gate verify` kanıtlanabilir biçimde değişmeyeni yeniden kullanır: baseline'ı zaman aşımına uğrayan ya da 120 sn ve üstü
süren kapı yeniden çalıştırılmaz ("failed at baseline" diye gösterilir), `paths` tanımlı ve dosyası değişmemiş kapı son
geçen verify'dan devralınır ("PASS (carried)"), baseline'da hızlı kalan kapı kısaltılmış zaman aşımıyla yeniden çalışır.
`--resume` aynı değişiklik için bitmiş kapıları atlar; `--all`, `ORKESTRA_GATES_FULL=1` ya da
`gates_rerun_failed_baseline` her şeyi baştan çalıştırır. `gates_parallel` 1'den büyükse typecheck, lint ve test kapıları
yan yana çalışır (en çok CPU sayısının yarısı).

**Bu işle gelen yapılandırma anahtarları** (`~/.orkestra/config.json`, varsayılanlar `paths.DEFAULT_GLOBAL_CONFIG` içinde):

| Anahtar | Varsayılan | Anlamı |
|---|---|---|
| `tempo.max_dispatches` | `3` | `next` `escalate` demeden önce yeni kanıtsız aynı görevlendirme sayısı. |
| `watchdog.agent_silent_seconds` | `150` | Yaşam belirtisi vermeyen kayıtlı alt-ajan bu süreden sonra kayıp sayılır. |
| `watchdog.autostart` | `true` | SessionStart hızlı izleyiciyi başlatabilir. |
| `watchdog.daemon_idle_minutes` | `30` | Ayrık izleyici açık run ve canlı oturum yokken bu kadar sonra çıkar. |
| `gates_parallel` | `2` | Eşzamanlı typecheck, lint ve test kapıları (projenin `config.json` değeri öncelikli). |
| `gates_rerun_failed_baseline` | `false` | `true`: `gate verify` baseline'da düşen ya da zaman aşımına uğrayan kapıları yeniden çalıştırır. |
| `headless.max_budget_usd` | `0` | 0'ın üstünde, başsız turlar için harcama tavanı. |
| `bridge.turn_timeout_seconds` | `7200` | Bir bridge turunun duvar saati bütçesi. |
| `bridge.turn_idle_seconds` | `1800` | Bu süre run hareketi olmayan bridge turu kesilir ve sürdürülür. |

`config.json` içinde yanlış tipte bir değer varsayılan lehine yok sayılır (`doctor` adını söyler).

## Kapsam

Orkestra, hook'ları etkin Claude Code, Cursor ve GitHub Copilot oturumlarında çalışır; tam akış ancak bir run
başlatıldığında devreye girer. İlk kurulum: `orkestra setup` (veya `cursor install` / `copilot install`).

- `/orkestra:run` (Claude/Copilot) veya `/orkestra-run` (Cursor) ile ve bridge üzerinden gelen istek: tam akış.
- Run başlatmadan yazılan istek: yalnız "her oturumda" guard'ları. Plan, onay ve TDD yoktur.
- Açık run varken aynı projede run'a bağlı olmayan ikinci oturum: uyarı alır ama dosyaları değiştirebilir.
- Hook'suz Copilot oturumu, Cursor'ın Orkestra dışı ajanı, Codex, Gemini CLI, claude.ai sohbeti ve Cowork: kapsam dışı.
- Bridge (Telegram/n8n/OpenClaw/local workbench) Claude Code, Cursor ve Copilot motorlarını başlatır
  (`/yap copilot: …` ya da `/run copilot: …`, `POST /jobs` ile `"engine": "copilot"`).

## Koordinasyon

Bu klasörü üç agent günceller: orkestra-core (eklenti ve bridge), openclaw-agent (`integrations/openclaw/`),
ecc (`vendor/ecc`, isteğe bağlı playbook'lar, bu belge). Kurallar `CONTRIBUTING.md`'de;
en önemlileri: açık run varken eklenti dosyası değiştirilmez; değişiklikten önce yedek ve md5 alınır, iş ayrı bir
kopyada yapılıp testler orada koşulur, canlı klasöre tek seferde aktarılır; commit, push ve branch yalnız insan kullanıcı / depo sahibinin açık isteğiyle yapılır.

CLI'ye yeni seçenek ya da alt komut eklenirken tek yer `lib/orkestra_core/cliparse.py`'dir: guard'lar komut satırını
oradaki ayrıştırıcıyla okur.

## Bakım ve geri alma

Test:

```bash
cd ./plugins/orkestra/tests && python3 -m unittest discover -s . -q
cd ./bridge/tests && python3 -m unittest discover -s . -q
cd ./integrations/openclaw/plugin && npm test
python3 ./plugins/orkestra/bin/orkestra doctor
```

Claude Code, `orkestra setup`'ın `~/.claude/plugins/cache/` altına (sürüme göre anahtarlanmış) önbelleğe aldığı kopyayı
çalıştırır; bu yüzden kod değişikliği ona ancak sürümü yükseltip `orkestra setup` çalıştırınca ulaşır. Cursor ve Copilot
her çağrıda checkout'u okur. Ajan ve skill metinleri ile `hooks.json` yeni açılan Claude Code oturumunda geçerli olur. Bridge uzun yaşayan bir
süreçtir; bridge kodu değiştiyse yeniden başlatılır. `install.sh` yalnız ilk kurulum, MCP ekleme ve testleri bu
makinede koşmak için gerekir.

Bir sürümü geri almak (açık run yokken): değişiklik günlüğü her sürümü adıyla verir; o sürümü çıkaran commit'e geçin.
Depoda sürüm etiketi yoktur.

```bash
python3 plugins/orkestra/bin/orkestra cursor uninstall   # yalnız 1.5.0'ın altına inerken (Cursor desteği 1.5.0 ile geldi)
git log --oneline -- CHANGELOG.md                        # istenen sürümün commit'ini bul
git checkout <commit>                                    # (ileride sürüm etiketleri olursa: git fetch --tags && git checkout v1.4.0)
cd plugins/orkestra/tests && python3 -m unittest discover -s . -q
```

Depoyu git ile almadıysan istediğin commit'i GitHub'dan ZIP olarak indir ve aynısını onunla yap. Bir run'ın proje
dosyalarında yaptığı değişikliği geri almak ayrı bir iştir: `/orkestra:rollback`.

Sabitlenmiş referansları yenilemek: `bash install.sh --refresh-vendor` (kaynak ve commit'ler `vendor/PINNED.md`'de).

## Açık konular

- CI matrisi Python 3.9, 3.11, 3.13 ve 3.14 için Linux, macOS ve Windows'ta yapılandırıldı; tam yerel test paketi Python 3.14.7'de koştu.
- Cursor motoru komut satırı aracıyla (`agent` 2026.10.01) beş gerçek XS run'da denendi; Cursor IDE penceresi ayrıca
  denenmedi (aynı hook sözleşmesi; IDE'ye özgü bir fark gözlenmedi çünkü bakılmadı).
- Cursor, Claude Code'un eklenti kaydındaki eski orkestra kopyasını (1.0.0, `~/.claude/plugins/cache/…`) da yükler;
  o kopyanın hook'ları Cursor sohbetlerinde eski kurallarla cevap verir (ör. `klasör/**` Grep'ini reddeder).
  `claude plugin update orkestra@orkestra-local` kaydı yeniler; Claude Code'un çalıştırdığı kopyaya etkisi denenmedi.
- Kendi kendini onaylama gerçek bir run'da görüldü ve üç katmanla kapatıldı (CHANGELOG 1.5.0); ortamını temizleyip
  üst süreçlerinden kopan, adları gizleyen bir betik hâlâ aşabilir. Claude Code tarafında hook'un "beni ajan başlattı"
  denetimi yok (Claude Code'un süreç düzeni ölçülmedi); komut ve içerik denetimi iki motorda da var.
- Cursor run'larında token sayımı ve implementer için bitiş anındaki TDD kanıtı uyarısı yok.
- Token maliyeti (run başına 76–546 milyon cache okuma) ölçülüyor ama azaltılmadı.
- Kapsam boşlukları yukarıdaki gibi duruyor.
