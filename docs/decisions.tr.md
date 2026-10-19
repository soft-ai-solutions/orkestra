# Kararlar ve sınırlar

[English](decisions.md)

Sahibin bana bıraktığı seçimlerin kısa kayıtları; 2026-10-19 itibarıyla (sürüm 1.6.0). Her madde neyin kararlaştırıldığını,
nedenini ve açıkça **yapılmayanı** söyler. Buradaki hiçbir şey gelecek için söz değildir.

## 1. Dağıtım

- **Karar.** Desteklenen yollar: git checkout ile `python3 install.py` ve deponun `pyproject.toml` dosyasından yalıtılmış
  bir Python kurulumu (`pipx install`, `uv tool install`, `uvx --from`).
- **Burada kanıtlandı.** `uvx --from <yerel checkout> orkestra --version` `orkestra 1.6.0` yazdı, `orkestra doctor` eklenti
  kökünü buldu ve hook öz sınamasını geçti, `uv tool install <yerel checkout>` `orkestra` komutunu kurdu. Checkout'un
  temiz bir sanal ortama `pip install` ile kurulumu da çalıştı. Hepsi geçici bir home içinde koştu.
- **Kanıtlanmadı.** Test makinesinde `pipx` yok; `pipx` komutu sınanmadı. `git+https://` biçimi aynı paketi derler ama
  yalnızca `pyproject.toml` ve `packaging/` işaret ettiğin uzak dalda varsa çalışır. `uvx` geçici bir önbellekten çalışır;
  `orkestra setup` komutunu `uvx` ile değil kalıcı bir kurulumdan (`uv tool install`, `pipx install`) ya da checkout'tan
  çalıştır. Claude Code kurulum adımı marketplace'i checkout'tan kaydeder, yani checkout ister; Cursor ve Copilot
  hook'ları ile CLI kurulu paketten çalışır.
- **Yapılmadı.** PyPI yüklemesi, Homebrew formülü, marketplace listelemesi yok. Her biri sahibin kayıt defteri
  hesaplarını, o kayıt defterinde isim çakışması kontrolünü ve açık onayı ister.
- **Sürüm betiği.** `scripts/release.sh` varsayılan olarak kuru koşudur. `--apply` ile sürümü tek geçişte günceller,
  changelog başlığını yazar ve `SHA256SUMS` ile `orkestra-X.Y.Z.tar.gz` üretir. `gh release create --draft` komutunu
  yazdırır, asla çalıştırmaz. Yayın elle, sahibin yaptığı bir adım olarak kalır.

## 2. Marketplace kimliği

- **Karar.** Claude Code marketplace kimliği `orkestra-local`, eklenti `orkestra@orkestra-local` olarak kalır.
- **Neden.** Adı değiştirmek mevcut her kurulumu ve bu adı anan her `enabledPlugins` kaydını sahipsiz bırakır.
- **Eklendi.** Marketplace ve eklenti manifestlerine açıklayıcı metadata (homepage, repository, license, keywords,
  category). Sürüm alanı yok: sürümün kaynağı changelog ve `__version__`.

## 3. Lisans, bildirimler, isim

- **Karar.** Lisans MIT olarak kalır.
- **Kontrol edildi.** `THIRD_PARTY_NOTICES.md`, `plugins/orkestra/vendor/PINNED.md` ile aynı iki projeyi, commit'i ve lisansı
  listeler (superpowers `8ca22db`, ECC `ef648e0`, ikisi de MIT, her klasörde kendi `LICENSE` dosyası). Depoda başka
  kopyalanmış üçüncü taraf kod yok: bridge arayüzü ve belgeler dış betik yüklemez, OpenClaw eklentisinin çalışma zamanı
  bağımlılığı yoktur.
- **Yapılmadı.** "Orkestra" adı marka veya paket kayıt defterleri açısından **kontrol edilmedi**. Herhangi bir herkese açık
  listelemeden önce yapılmalı.

## 4. Güvenlik varsayılanları

- **Copilot allow-all varsayılan olarak AÇIK** (`--allow-all-tools --allow-all-paths --allow-all-urls`), çünkü sahibi
  böyle seçti: başsız bir run bir soruda takılmamalı. Bu bir güvenlik özelliği değildir, Copilot'un kendi sorularını
  kaldırır. Orkestra'nın hook'ları (Copilot `PreToolUse` için fail-closed), guard'ları ve hariç tutulan araçlar yine
  geçerlidir, ama guard'lar filtredir, sandbox değil (README'de "Bilmen gereken sınırlar").
- **Görünür.** `orkestra copilot status` ve `orkestra run --engine copilot` başlangıcı, açıkken bir uyarı yazar.
- **Kapatma.** Tek run için: `orkestra run --engine copilot --no-allow-all`. Kalıcı olarak: `~/.orkestra/config.json`
  içinde `engine.copilot.allow_all` değerini `false` yap. İstekteki "no allow all" gibi ifadeler de düşürebilir; asla
  yükseltemez. Kapalıyken yalnızca `--allow-all-tools` geçirilir.
- **MCP.** Kurulum `@playwright/mcp` paketini `@latest` yerine kesin bir sürüme sabitler ve sorulardan önce Context7'nin
  sorgularını alan üçüncü taraf bir uç nokta olduğunu söyler. `install.sh --no-mcp` tüm MCP adımlarını atlar; mevcut
  kayıtlar asla değiştirilmez.
- **Testler.** Kurulum betikleri test takımlarını varsayılan olarak atlar. `--with-tests` önce onları koşar (yaklaşık on
  dakika); `--skip-tests` kabul edilir ve hiçbir şey yapmaz.

## 5. Maliyet

- Orkestra'nın kendi başına model token'ı harcayan hiçbir parçası yok. Token'ı senin başlattığın motorlar harcar:
  `orkestra run`, eklentili bir sohbet oturumu, gönderdiğin bir bridge işi ve bu run'ların devamları (watchdog başsız
  run'ları en çok `max_auto_resumes` kez, varsayılan 3, sürdürür; durdurmak için `watchdog.auto_resume` değerini `off` yap).
- **Yapılmadı.** Kıyaslamalar (istek türü başına maliyet ya da kalite) ve model kademelendirme (kolay fazlarda ucuz,
  incelemede güçlü model). İkisi de gerçek motorlarda faturalı koşular ister. Gerekenler: bilinen iyi sonuçlu 10-20
  sabit istek, aynı kümenin motor ve model başına koşulması, her motorun kendi kullanım raporundan token ve süre
  değerleri (Cursor rapor vermez) ve sahibinin bütçesi. O zamana dek maliyet ya da kalite iddiası yapılmaz.

## 6. Gizlilik

- Telemetri yok, analitik yok, güncelleme denetimi yok. Orkestra kendi başına dışarı bağlantı açmaz.
- Makineden ne çıkabilir, kim gönderir:
  - **Motor sağlayıcıları** (Anthropic, Anysphere, GitHub ve arkalarındaki model sunucuları) çalıştırdığın istemciden
    prompt ve proje bağlamı alır, kendi koşullarına göre.
  - **Context7** yalnızca o MCP sunucusunu kaydettiysen, asistanının baktığı kütüphane adlarını ve konuları alır.
  - **npm**, kaydettiysen ilk kullanımda `@playwright/mcp` paketini sunar. Kaynaktan pip, pipx ya da uv ile kurarken
    paket dizinleri derleme araçlarını sunar.
  - **GitHub**'a `install.sh --refresh-vendor` ve `git clone` bağlanır.
  - **Telegram, `callback_url`, `notify.webhook_url`** yalnızca sen yapılandırırsan.
- Run durumu, günlükler ve bridge token'ı `<proje>/.orkestra/` ve `~/.orkestra/` içinde kalır.
