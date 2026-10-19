# İlk kurulum: Orkestra makinende

[English](ONBOARDING.md) · Türkçe

Ürün özeti: [README.tr.md](README.tr.md) · tek sayfalık başvuru: [ORKESTRA.tr.md](ORKESTRA.tr.md) · bu depoyu düzenleyen
ajanların kuralları: [AGENTS.md](AGENTS.md).

Bu sayfa yeni bir makineyi sıfırdan, macOS, Linux ya da Windows'ta ilk denetimli run'a götürür.

## 1. Başlamadan önce

Gerekenler:

- **Python 3.9 ya da üstü**, yalnız standart kütüphane. `python3 --version` ile bak (Windows: `py -3 --version`).
- **git**.
- **En az bir motor**: Claude Code (`claude`), Cursor CLI (`agent`), GitHub Copilot (`copilot` CLI'si veya VS Code
  ajanı) ya da Pi (`pi` CLI'si veya `~/.pi/agent`). Orkestra onları sürer; yerlerine geçmez.
- **Yalnız Windows**: Claude Code hook'ları için PATH'te POSIX bir `sh`. Git for Windows bunu sağlar. macOS ve Linux'ta
  zaten vardır.

## 2. Kodu al

```bash
git clone https://github.com/soft-ai-solutions/orkestra.git
cd orkestra
```

`orkestra setup`, Claude Code eklentisini `plugins/orkestra` altından `~/.claude/plugins/cache/` içine kopyalar; Claude Code o kopyayı çalıştırır. Checkout'u bulunduğu yerde bırak: Cursor ve Copilot doğrudan onu okur.

## 3. Kur

macOS ve Linux:

```bash
bash ./install.sh --dry-run         # isteğe bağlı: planı yazdır, hiçbir şeyi değiştirme
bash ./install.sh                   # etkileşimli; --yes varsayılanları kabul eder, --no-mcp isteğe bağlı MCP sunucularını atlar
bash ./install.sh --with-tests      # önce eklenti ve bridge test takımlarını da koş (yaklaşık on dakika)
```

Windows (PowerShell ya da cmd), native, WSL gerekmez:

```powershell
.\install.ps1 --dry-run
.\install.ps1
python install.py --yes --with-tests
```

`install.ps1` tüm argümanları `install.py`'ye iletir; `install.py` motor kurulumunu, PATH kısayolunu ve sağlık kontrolünü
her sistemde yapar. İsteğe bağlı MCP adımı ve bridge token adımı yoktur (`--no-mcp` kabul edilir ve yok sayılır; token'ı
`python bridge/orkestra_bridge.py init` ile oluştur), `--refresh-vendor` da yoktur. PowerShell betiği çalıştırmayı reddederse
(execution policy) ya da `python` Microsoft Store'u açarsa `py -3 install.py` çalıştır. LaunchAgent
(`launchd`) adımı yalnız macOS'a özgüdür ve Linux ile Windows'ta atlanır.

Kurulum, `--with-tests` vermezsen test koşmaz; verirsen önce eklenti ve bridge test takımlarını makinende koşar (yaklaşık
on dakika; CPU yükü süreyi değiştirir) ve biri bile kırmızıysa durur (`--skip-tests` kabul edilir ve bir şey yapmaz). Sonra checkout'u `orkestra-local` adlı Claude Code marketplace'i olarak kaydeder,
`orkestra@orkestra-local` eklentisini kurar, `orkestra doctor` çalıştırır, Cursor ve Copilot kurulumunu önerir, PATH
kısayolunu ekler ve `install.sh` ile isteğe bağlı MCP sunucularını ve bridge token'ını önerir. Claude Code CLI yoksa yalnız
Cursor ve Copilot ile devam eder ve hiçbir motor hazır değilse uyarır. Başarısız bir Claude Code adımı ya da başarısız bir
gerekli sağlık kontrolü kurulumu 1 çıkış koduyla bitirir. `install.py --dry-run` Pi'yi de planlar; `install.sh` ve
`install.ps1` Pi'yi `orkestra setup` ya da `orkestra pi install` komutuna bırakır.

## 4. `orkestra`'yı PATH'e koy

`orkestra setup` ve kurulum betikleri `orkestra` adlı bir kısayol yazar, ama yalnız zaten var olan bir klasöre:

| Sistem | Kısayol | Klasör |
| --- | --- | --- |
| macOS, Linux | `orkestra` (symlink) | `~/.local/bin` |
| Windows | `orkestra.cmd` | Varsa `%LOCALAPPDATA%\Microsoft\WindowsApps` (varsayılan olarak PATH'tedir), yoksa `%USERPROFILE%\.local\bin` |

`ORKESTRA_SHIM_DIR` klasörü her yerde geçersiz kılar. Klasör henüz yoksa oluştur, PATH'ine ekle ve setup'ı yeniden
çalıştır. Kısayol olmadan kısa komut yoktur; bu yüzden yoluyla başlat:

```bash
mkdir -p ~/.local/bin               # sonra shell profiline PATH olarak ekle
python3 plugins/orkestra/bin/orkestra setup
```

Windows'ta `%USERPROFILE%\.local\bin` klasörünü kullanıcı PATH'ine ekle (Ayarlar, sonra Ortam Değişkenleri). Developer Mode
isteğe bağlıdır: açıksa Orkestra skill ve ajanlar için symlink kullanabilir, kapalıyken dosyaları kopyalar ve `.cmd`
kısayolunu yazar. Kısayol PATH'e girene kadar CLI'yi yoluyla çağır: `python3 plugins/orkestra/bin/orkestra <komut>`
(Windows: `plugins\orkestra\bin\orkestra.cmd <komut>`). Claude Code oturumunun içinde `orkestra` zaten çalışır.

## 5. Motorlarını etkinleştir

```bash
orkestra setup                # Claude Code, Cursor, Copilot ve Pi'yi bulur; her birini açmadan önce sorar
orkestra setup --yes          # bulunan her motoru soru sormadan etkinleştirir
```

Motor başına, `setup`'ın yaptığıyla aynı şey:

```bash
orkestra cursor install       # ~/.cursor altında skill ve alt-ajanlar, hook girdileri ~/.cursor/hooks.json'a birleştirilir
orkestra cursor status
orkestra copilot install      # Copilot hook'ları $COPILOT_HOME (ya da ~/.copilot) altında
orkestra copilot status
orkestra pi install           # skill'ler ~/.pi/agent/skills altında
orkestra pi status
```

`cursor install --dry-run` neyi değiştireceğini gösterir; `--copy` bağlamak yerine kopyalar. `cursor uninstall` yalnız
eklediği girdileri kaldırır ve önce `hooks.json` için zaman damgalı bir yedek alınır.

`pi install --dry-run`, `~/.pi/agent/skills` altına yazacağı skill dosyalarını ve `~/.pi/agent/extensions` altına
yazacağı eklentiyi gösterir. `pi uninstall` yalnız işaretlediği dizinleri ve eklentiyi kaldırır. Eklenti, bağlı
bir koşuda araç çağrılarını denetler. Skill'ler ve eklenti kurulunca `orkestra run --engine pi`, Pi'yi
`/skill:orkestra-run` ile başlatır.

### Copilot host'unda etkinleştirme (elle)

`orkestra copilot install`, Copilot eklentisi için Orkestra hook girdilerini yazar. Bu depoda host'un kendi anahtarlarını
(VS Code GitHub Copilot ajan eklentisi, oturum hook'ları, model seçici) çevirecek **desteklenen bir API yoktur**. Kurulumdan
sonra:

1. `orkestra copilot status` çıktısının hook'ların etkin olduğunu söylediğini doğrula.
2. Eklentiyi Copilot host'unda yükle ki `SessionStart`, `UserPromptSubmit`, `PreToolUse` ve `PostToolUse` `copilot_hook.py`
   dosyasını çalıştırsın. `orkestra run --engine copilot` bunu senin yerine yapar: Copilot CLI'yi, kurulu eklentiyi
   (`$COPILOT_HOME/plugins/orkestra`, varsayılan `~/.copilot/plugins/orkestra`) gösteren `--plugin-dir` ile başlatır. Elle
   açtığın bir Copilot CLI oturumunu da aynı şekilde başlat: `copilot --plugin-dir ~/.copilot/plugins/orkestra`
   (`copilot plugin install` yalnız marketplace, GitHub ya da git kaynağı alır, yerel yol almaz). VS Code ajanında böyle
   bir bayrak yok: eklentiyi ayarlarından kendin etkinleştir.
3. Hook'suz bir Copilot oturumundan denetimli bir Orkestra run'ı başlatma ya da sürdürme.

**Model, efor, context, allow-all.** CLI run'ları (`orkestra run --engine copilot`, bridge, watchdog resume)
varsayılan olarak otomatiktir: `gpt-6-luna`, efor `max`, `long_context`, allow-all. Run başına `--model`, `--effort`,
`--context`, `--no-allow-all` (yine `--allow-all-tools` geçer, yalnız yol ve URL düşer) ile ya da
`~/.orkestra/config.json` içindeki `engine.copilot` ile değiştir; `orkestra copilot status` sonucu ve kaynağını gösterir.
`gpt-6-luna` yapılandırılmış varsayılandır, doğrulanmış değildir: giriş yapılan hesabın kullanabildiği modeller
çevrimdışı denetlenemez, yoksa ilk kullanımda CLI'nın kendi hatası görünür; `--model` ya da `chat.defaultModel` bunu
geçersiz kılar. VS Code'da efor ve context boyutu için ayar yoktur (yalnız sohbet model seçici); varsayılan model
(`chat.defaultModel`) ve izinler (`chat.defaultConfiguration`) için vardır. VS Code 1.141'de `chat.plugins.enabled`
varsayılan olarak **açıktır**, yani eklentinin yalnız kaydedilmesi gerekir: `orkestra copilot vscode --user` ayarları
gösterir, `--apply` yazar (`chat.pluginLocations` makine kapsamlıdır, `--workspace` eklentiyi kaydedemez; VS Code'un
araç onayı sormayı bırakmasını istiyorsan ayrıca `--permissions autopilot` ekle; kurumsal politika yine Manual'a
zorlayabilir). `--remove` yalnız Orkestra'nın yazdığı değerleri geri alır.

Gelecekte bir host kararlı bir etkinleştirme API'si sunarsa burada belgele.

## 6. Makineyi denetle

```bash
orkestra doctor
```

Gerekli kontroller geçtiğinde ve en az bir motor hazırsa 0, aksi halde 1 ile çıkar. `[OK]` satırları geçti, `[--]` satırları
isteğe bağlı ya da bilgi amaçlıdır (MCP sunucuları, kullanmadığın bir motor). Önce bakılacak satırlar:

- `hook self-test (deny force-push)`: hook yasaklı bir komutu gerçekten reddediyor. Bu kırılırsa guard'lar etkin değildir;
  düzelene kadar run başlatma.
- `python3 on PATH (plugin hooks)`: bilgi amaçlı. Claude Code hook'ları `sh hooks/run_hook.sh` üzerinden başlar; bu betik
  `python3`, `python` ve `py -3` komutlarını dener. Windows'ta bu satır `not found` diyebilir, `python` ya da `py` yine de
  çalışıyor olabilir.
- `engine ready`: Claude Code, Cursor hook'ları, Copilot hook'ları, Pi'den en az biri.
- `language`: Orkestra'nın hangi dili ve neden kullanacağı (sonraki adım).

## 7. Dili seç

Varsayılan dil İngilizcedir; sen aksini söylemedikçe Orkestra işletim sistemi dilini izler.

```bash
orkestra lang show            # kullanılan dil ve nereden geldiği
orkestra lang tr              # Türkçeyi ~/.orkestra/config.json içine kaydet  (en | tr | auto)
```

Tek komut için `--lang tr`, tek shell oturumu için `ORKESTRA_LANG=tr` kullan. Sıra README'de
([Dil](README.tr.md#dil)).

## 8. İlk run

Projeler çalışma klasörü altında adıyla aranır; varsayılan `~/Projects`. Değiştirmek için `~/.orkestra/config.json`
içindeki `"workspace_root"` ya da `ORKESTRA_WORKSPACE` ortam değişkenini kullan; yol vermek her zaman çalışır.

```bash
orkestra init -p <proje>      # isteğe bağlı: .orkestra/ oluşturur, test, lint, typecheck ve build kapılarını bulur
orkestra run -p <proje> "yapacağın en küçük güvenli değişikliği anlat"
orkestra watch                # ikinci bir terminalde: faz, şu an ne yaptığı, kimi beklediği
```

Ajan sohbetinde aynı istek `/orkestra:run <istek>` olur (Cursor: `/orkestra-run`). Durdurmak için host'un durdurma
düğmesini, `orkestra pause -p <proje>` ya da `orkestra abort -p <proje> --reason "..."` komutunu kullan; proje dosyaları
olduğu gibi kalır.

## Güncelleme ve kaldırma

Güncellemek için checkout'ta `git pull` yap, sonra sürümü yükseltip `orkestra setup` çalıştır: Claude Code,
`~/.claude/plugins/cache/` altındaki sürüme göre anahtarlanmış kopyayı çalıştırır; yalnız `git pull` orada hiçbir şeyi
değiştirmez (Cursor ve Copilot checkout'u okur). Ajan, skill ve `hooks.json` değişiklikleri yeni açılan Claude Code
oturumunda geçerlidir. Cursor tarafını
`orkestra cursor status` ile denetle; bridge kullanıyorsan yeniden başlat (uzun ömürlü bir süreçtir).

```bash
bash ./install.sh --uninstall           # macOS ve Linux; ~/.orkestra'yı da silmek için --purge ekle
python install.py --uninstall --dry-run # her sistemde: önce nelerin kaldırılacağını yazdır
```

Kaldırma, Claude Code eklentisini ve marketplace'ini, Cursor ve Copilot hook girdilerini, PATH kısayolunu ve macOS bridge
LaunchAgent'ını siler. `install.sh`'nin eklediği MCP sunucularına dokunmaz (`claude mcp remove <ad>` ile kaldır). `--purge`
Orkestra klasörünü (ayarlar, run geçmişi, bridge token'ı) yalnız onayladığında ya da `--yes` verdiğinde siler.
Projelerinin içindeki `.orkestra/` klasörlerine hiçbir zaman dokunulmaz. Artık çözümlenemeyen bir `hooks.json`
kaldırmayı 1 çıkış koduyla durdurur ve hiçbir şeyi değiştirmez: dosyayı düzeltip yeniden çalıştır.

## Bir şey ters giderse

| Belirti | Ne yapmalı |
| --- | --- |
| `orkestra: command not found` | 4. bölüm: kısayol klasörünü oluştur, PATH'e ekle, `python3 plugins/orkestra/bin/orkestra setup`'ı yeniden çalıştır. |
| `doctor` hook öz-testinde kırılıyor | `orkestra setup`'ı yeniden çalıştır; Windows'ta PATH'te `sh` ve gerçek bir Python 3.9+ (Store taslağı değil) olduğunu denetle. |
| Cursor ya da Copilot guard'ları çalışmıyor | `orkestra cursor status` / `orkestra copilot status`, sonra **yeni** bir sohbet aç; Copilot için host eklentisini etkinleştir (5. bölüm). |
| Kurulum kırmızı bir testte duruyor | Yalnızca `--with-tests` ile olur. Kırılan test ve `orkestra doctor` çıktısıyla bir issue aç. |
| Çıktı yanlış dilde | `orkestra lang show`, sonra `orkestra lang en` ya da `orkestra lang tr`. |

Daha fazlası README'de: [SSS ve sorun giderme](README.tr.md#sss-ve-sorun-giderme).
