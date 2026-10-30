# OpenClaw → orkestra sözleşmesi (orkestra-core yazar, stabil)

English: [CONTRACT.md](./CONTRACT.md) (kanonik sürüm; bu belge onun Türkçesi, ikisini birlikte güncelle).

OpenClaw orkestrayı **bridge** üzerinden çağırır. OpenClaw'ın kendi config/skill yapısı openclaw-agent'ın alanıdır;
bu belge yalnız orkestra tarafını tanımlar.

## Önkoşullar (aynı Mac)

- `bash ./install.sh` (veya `$ORKESTRA_ROOT/install.sh`; plugin + `~/.orkestra/bridge.secret.json` token)
- Bridge açık: `python3 ./bridge/orkestra_bridge.py serve` (macOS'ta ayrıca `... launchd`)
- `python3 ./bridge/orkestra_bridge.py doctor` → hazır

## Telegram tek-tüketici kuralı

OpenClaw Telegram botunu zaten dinliyorsa **bridge Telegram'ı dinlemez** (`telegram.bot_token` boş kalır).
Kullanıcının mesajları OpenClaw'a gelir; OpenClaw orkestra komutlarını aşağıdaki istemciyle bridge'e iletir.

## Önerilen komut dili (kullanıcıya görünen)

Her komutun bir Türkçe, bir İngilizce adı vardır; ikisi de her iki dilde her zaman çalışır.

| Kullanıcı yazar                                              | OpenClaw çalıştırır                                                              |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| `/yap <proje> <istek>` (İngilizce: `/run`)                   | Bridge'in varsayılan engine'iyle yeni iş başlat                                  |
| `/yap claude: <proje> <istek>`                               | Claude Code seç                                                                  |
| `/yap cursor: <proje> <istek>`                               | Cursor CLI seç                                                                   |
| `/yap copilot: <proje> <istek>`                              | Copilot CLI seç                                                                  |
| bekleyen bir soruya/onaya cevap (ör. `onay`)                 | `python3 ./bridge/orkestra_client.py reply --chat-id <chat> "<metin>"`           |
| `/durum` (İngilizce: `/runs`)                                | `python3 ./bridge/orkestra_client.py status --chat-id <chat>`                    |
| `/iptal` (İngilizce: `/cancel`)                              | `python3 ./bridge/orkestra_client.py cancel --chat-id <chat>`                    |
| `/canli` (öneri; `/durum` da içerir; İngilizce: `/live`)     | `python3 ./bridge/orkestra_client.py runs` → düz metin; `--json` ile yapı        |

Birlikte gelen OpenClaw eklentisi (`integrations/openclaw/plugin`) altı adı da kaydeder. `/durum`'un İngilizce adı
`/runs`'tır çünkü OpenClaw `/status`'u kendine ayırmıştır; host'un reddettiği bir ad uyarıyla atlanır, eklentiyi durdurmaz.

Alternatif (tek uç nokta): her mesajı olduğu gibi `POST /telegram {"chat_id", "text"}` ile ilet; yanıttaki `message`
alanını kullanıcıya gönder. Bridge komutları ve cevapları kendisi ayırt eder: `/yap` veya `/run`, `/durum` veya
`/status`, `/iptal` veya `/cancel`, `/yardim` veya `/help`; geri kalan her metin kullanıcıyı bekleyen işe cevaptır.

## Dil

Bir insanın okuduğu her şey (Telegram yanıtları, bildirimler, web arayüzü, CLI çıktısı) İngilizce ya da Türkçedir,
varsayılan İngilizce. Bridge, CLI ve eklenti için tek ayar belirler; ilk bulunan kazanır:

1. `ORKESTRA_LANG` ortam değişkeni (`en` | `tr` | `auto`)
2. `~/.orkestra/config.json` içindeki `"lang"` (`en` | `tr` | `auto`; çalışan bridge ve eklenti değişikliği görür)
3. işletim sistemi dili (`LC_ALL`, `LC_MESSAGES`, `LANG`, `LANGUAGE`; `tr...` dili Türkçeyi seçer)
4. İngilizce

Dile göre **değişmeyenler**: HTTP API'nin her JSON anahtarı ve enum'u (`status`, `phase`, ...), HTTP `error` kodları
(`unauthorized`, `not found`, ...), yukarıdaki komut sözlüğü ve modelin okuduğu metin.

Protokol belirteçleri iki dilde de kabul edilir; bir dilde başlayan çalışma diğerinde sürdürülebilir:

- Bildirim başlığı İngilizcede `[proje] <durum etiketi> · <faz> · job <id>`, Türkçede `... · iş <id>`; başlatma onayı
  `Started: [proje] job <id>` / `Başladım: [proje] iş <id>`. Bir Telegram yanıtını işine yönlendiren istemci iki iş
  sözcüğünü de (`job` | `iş`) ve ardından 12 haneli hex iş kimliğini kabul etmelidir; eklenti eder (`parseJobRef`).
- Seçilen onay seçeneği kendi dilinin onay sözcüğü olarak iletilir (`approve` / `onayla`); orkestra ikisini de kabul
  eder. Koşullu seçenekler olduğu gibi kalır.

## Bildirimler (orkestra → kullanıcı)

İş soru sorduğunda, onay beklediğinde, bittiğinde ya da engellendiğinde bridge bir bildirim üretir:

- `bridge.secret.json → callback_url`: OpenClaw'ın gelen-webhook adresi verilirse bridge şu JSON'u POST eder:
  `{"id","project","status","phase","run_id","chat_id","message"}`. `message` kullanıcıya olduğu gibi gönderilecek metindir
  (bridge'in dilinde). `status`: `awaiting_user` (cevap bekliyor) · `done` · `blocked` · `cancelled` · `error`.
- Webhook mümkün değilse OpenClaw periyodik olarak `orkestra_client.py status --chat-id <chat>` sorgular
  ve `updated` değişen işlerin `last_message`'ını iletir.

## HTTP API (127.0.0.1:47615, `Authorization: Bearer <bridge.secret.json → token>`)

- `POST /jobs {project, request, chat_id?, source?, engine?}` → 202 job · aynı projede açık iş varsa 400
  (`engine`: `claude` | `cursor` | `copilot`; varsayılan bridge/orkestra yapılandırmasından gelir)
- Her istek `?lang=en|tr` ekleyebilir: bridge'in o cevap için yazdığı metinler (hatalar, canlı durum, motor nedenleri)
  bridge'in kendi dili yerine o dili kullanır. JSON anahtarları, durum değerleri ve HTTP kodları hiç değişmez.
- `POST /jobs/<id>/reply {text}` · `POST /reply {chat_id, text}` → 202
- `GET /jobs[?chat_id=]` · `GET /jobs/<id>` · `GET /health` → `{"ok": true, "lang": "en" | "tr"}` (`lang` dil desteğiyle eklendi; eklemeli)
- `GET /runs` → `{"runs": [...]}` (1.2.0+, eklemeli): her açık çalışma için `project_name, run_id, phase, status,
  live{level,text}, tasks_done, tasks_total, activity[{time,agent,kind,tool,what}]`. `live.level`:
  `run|wait|quiet|stale|blocked|paused|done`; `wait` = kullanıcıdan cevap bekliyor.
- `POST /telegram {chat_id, text}` → `{message}` (yukarıdaki komut dili; `allowed_chat_ids` doluysa yalnız onlar)
- `GET /projects` → `{"projects": [{"name"}], "restricted": bool}`: iş başlatılabilecek klasörler (izin listesi
  uygulanmış). İstemci bunu `orkestra_client.py projects` ile yazar.

`orkestra_client.py` bir bağlantıyı yalnızca istek bridge'e hiç ulaşmadığında yeniden dener (0,3, 1 ve 2 sn sonra:
bridge yeniden başlarken bağlantı reddi, yoğun makinede boş yerel port kalmaması); bridge'e ulaşmış bir istek asla iki kez
gönderilmez. Yerel bridge için `HTTP(S)_PROXY` değişkenlerini yok sayar ve JSON olmayan bir yanıtta da her zaman JSON yazar.

## Doğal dildeki istekler (paketteki eklenti)

Komutların yanında eklenti OpenClaw ajanına iki araç verir: `orkestra_run {project, request, engine?}` ve
`orkestra_status {}`. Sahip "namaz vakitleri'nde bildirim sesini düzelt" yazınca ajan çalışmayı `orkestra_run` ile
başlatır; bu araç `/yap` ile aynı yoldan geçer: ad bir klasörle eşleştirilir (tek bir klasöre katlanan yazım düzeltilir,
yoksa adaylar listelenir), açık çalışması olan proje reddedilir (emin olunamazsa başlatılmaz) ve işin soruları Yanıtla
ile cevaplanan bildirimler olarak döner. Ajan bir çalışmanın sorusunu asla kendisi cevaplamaz. Araçlar
`openclaw.plugin.json` içinde (`contracts.tools`) bildirilir; sınırlı bir araç profili (ör. `coding`) onları modele
yalnızca `tools.alsoAllow` içindeyse sunar.

## Garantiler

- Headless run'lar etkileşimli olanla aynı guard'lardan geçer: commit/push/deploy yok, sırlar okunmaz; autopilot varsayılandır, manual plan onayı yalnız gerçek kullanıcı mesajıyla kaydedilir.
- Copilot bridge işleri etkin Orkestra hook plugin'iyle `copilot` CLI üzerinden başlar. Varsayılan profil `gpt-6-luna` / `max`; istekte tek bir desteklenen model/effort açıkça yazılırsa seçilir, geçersiz veya belirsiz seçim default'a döner.
- OpenClaw bunu kullanıcı adına göndermemeli.
- Proje başına tek açık run; ikinci istek 400 döner.
- `allowed_projects` (bridge.secret.json) ile uzaktan başlatılabilecek projeler sınırlanabilir.
