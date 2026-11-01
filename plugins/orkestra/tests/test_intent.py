import unittest

import helpers  # noqa: F401
from orkestra_core.intent import (approval, git_grants, infer_size, is_advisory_request, manual_approval_requested,
                                  min_context_chars, off_topic_vs_project, resolve_size, skips_adversary,
                                  skips_plan_critic,
                                  approval_answer, title_from_request)

GRANT_CASES = {
    "commitle ve pushla": ["commit", "push"],
    "değişiklikleri commitleyip pushlar mısın": ["commit", "push"],
    "son commit'te ne değişti?": [],
    "commit mesajı öner": [],
    "commitleme sakın, ben yapacağım": [],
    "commitlemeden önce testleri koş": [],
    "please commit and push": ["commit", "push"],
    "don't push yet, just commit": ["commit"],
    "Commit at ama push etme": ["commit"],
    "git push yap": ["push"],
    "bu dosyayı düzelt": [],
    "pushlamadan önce bana göster": [],
    "commit this": ["commit"],
    "the commit hash is abc": [],
    "/orkestra:ship": ["commit", "push"],
    "/orkestra:rollback": ["discard"],
    "yeni branch aç ve commitle": ["branch", "commit"],
    "branch açma": [],
    "PR aç": ["pr"],
    "force push yap": ["force", "push"],
    "commit history'yi incele": [],
    "Tüm değişiklikleri COMMİTLE": ["commit"],
    "commit sayısı kaç": [],
    "kodu yaz, sonra commit et": ["commit"],
    "commitlenmesin": [],
    "push the changes to origin": ["push"],
    "no commit please": [],
    "issue aç": ["gh_write"],
    "main'i merge et": ["sync"],
    "git pull yap": ["sync"],
    "v1.2 tag oluştur ve pushla": ["push", "tag"],
    "pull request'e bak": [],
    "shop'a dark mode ekle": [],
    "değişiklikleri geri al": ["discard"],
    "push bildirimi ekle": [],
    "push notification ekle": [],
    "Push servisini düzelt": [],
    "pushlandı mı?": [],
    "commitledin mi?": [],
    "commitler neler?": [],
    "commitleri listele": [],
    "main'e geç": ["branch"],
    "switch to main": ["branch"],
    "git stash yap": ["stash"],
    "amend the last commit": ["amend", "commit"],
    "v1.2 tagla": ["tag"],
    "son değişiklikleri çek": ["sync"],
    "pushla ama force push yapma": ["push"],
    "pushlar mısın?": ["push"],
    "reset --hard yap": ["discard", "hard_reset"],
}

APPROVAL_CASES = {
    "onaylıyorum": "approve",
    "Onay": "approve",
    "evet": "approve",
    "devam et": "approve",
    "LGTM": "approve",
    "onaylıyorum ama testleri ekle": "reject",
    "hayır": "reject",
    "T003'ü değiştir": "reject",
    "şu dosyayı da incele ve sonra plana bak lütfen bunu yapalım mı acaba": None,
    "": None,
    "onay vermiyorum": "reject",
    "Ok bekle": None,
    "Tamam yarın bakarım": None,
    "tamam mı sence bu plan?": None,
    "devam etmeden önce bir soru sorayım": None,
    "go back to the plan": None,
    "Onaylıyorum.": "approve",
    "evet başla": "approve",
    # Sentence-level approval (Cursor: user rarely types bare "onay")
    "onay verdim": "approve",
    "onay veriyorum": "approve",
    "onaylıyorum yap": "approve",
    "planı onaylıyorum": "approve",
    "bu planı da onaylıyorum yap en uzman şekilde": "approve",
    "bu planı da onaylıyorum yap en uzman şekilde en kapsamlı testlerle hiçbir sağlıklı yapıyı bozmadan yap": "approve",
    "onay veriyorum devam et": "approve",
    "I approve — ship it": "approve",
    "yap": None,
    "onaylıyor musun?": None,
    "onaylıyorum ama T003'ü çıkar": "reject",
}


class TestIntent(unittest.TestCase):
    def test_manual_approval_mode_requires_an_explicit_positive_request(self):
        cases = {
            "ben karar verici olmalıyım": True,
            "kararları ben vermeliyim": True,
            "approve aşamasında onayı bana sor": True,
            "I want to decide each approval": True,
            "let me approve the plan": True,
            "Default to autopilot unless I say 'let me decide': ask me then": False,
            "Varsayılan autopilot; kullanıcı isterse ben karar verici olmalıyım derse onay sor": False,
            "Don't ask me, just do it autonomously": False,
            "kararı sen ver, onay bekleme": False,
            "onay nedir?": False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(manual_approval_requested(text), expected)

    def test_approval_answers(self):
        from orkestra_core.intent import approval_answer
        self.assertEqual(approval_answer("Onayla — uygulamaya başla"), "approve")
        self.assertEqual(approval_answer("Onayla ama T3'ü çıkar"), "reject")
        self.assertEqual(approval_answer("Değişiklik istiyorum"), "reject")
        self.assertEqual(approval_answer("İptal et"), "reject")

    def test_deploy_requested_only_for_a_clear_ask(self):
        from orkestra_core.intent import deploy_requested, prompt_grants
        yes = [
            "tüm testler başarılıysa prod build ve deploy yap",
            "deploy et",
            "npm run deploy",
            "Deploy: kullanici kapilar yesil olunca uretim komutunu istedi.",
        ]
        no = [
            "deploy guard reddederse ship.md yaz",
            "deploy etme",
            "don't deploy",
            "add a deploy button to the settings page",
            "what is a deploy?",
        ]
        for text in yes:
            with self.subTest(text=text):
                self.assertTrue(deploy_requested(text))
                self.assertIn("deploy", prompt_grants(text))
        for text in no:
            with self.subTest(text=text):
                self.assertFalse(deploy_requested(text))
                self.assertNotIn("deploy", prompt_grants(text))

    def test_git_grants(self):
        for text, expected in GRANT_CASES.items():
            with self.subTest(text=text):
                self.assertEqual(git_grants(text), sorted(expected))

    def test_approval(self):
        for text, expected in APPROVAL_CASES.items():
            with self.subTest(text=text):
                self.assertEqual(approval(text), expected)



class TestOffTopic(unittest.TestCase):
    def _ws(self):
        from pathlib import Path
        import tempfile, os
        root = Path(tempfile.mkdtemp(prefix="ot-"))
        bound = root / "sample-app"
        other = root / "orkestra"
        bound.mkdir(); other.mkdir()
        os.environ["ORKESTRA_WORKSPACE"] = str(root)
        return root, bound, other

    def test_other_workspace_project_is_off_topic(self):
        import os
        root, bound, other = self._ws()
        try:
            self.assertTrue(off_topic_vs_project(
                "bu chatte orkestra projesi ile ilgili geliştirme yap", bound, "003-x"))
            self.assertFalse(off_topic_vs_project(
                "sample-app T041 devam", bound, "003-x"))
            self.assertFalse(off_topic_vs_project("devam et", bound, "003-x"))
            self.assertFalse(off_topic_vs_project(
                "Approve aşamasında onayı bana sor; ben karar verici olayım.", bound, "003-x"))
        finally:
            os.environ.pop("ORKESTRA_WORKSPACE", None)

    def test_unnamed_request_without_continue_is_off_topic(self):
        """2026-10-12 (BLK-02): 'make this dashboard friendlier' while bound used to unbind (no project name, no
        continue cue). An ordinary message now KEEPS the binding; only an explicit signal lets go of it
        (test_binding_model.TestOffTopic has the table)."""
        import os
        root, bound, _other = self._ws()
        try:
            self.assertFalse(off_topic_vs_project(
                "make this dashboard friendlier", bound, "003-demo-kayit",
                title="Demo Kayıt"))
            self.assertFalse(off_topic_vs_project(
                "bu paneli daha okunaklı yap", bound, "003-x"))
            self.assertTrue(off_topic_vs_project(
                "forget this run, make this dashboard friendlier", bound, "003-x"))
            self.assertFalse(off_topic_vs_project("continue from here", bound, "003-x"))
            self.assertFalse(off_topic_vs_project("/orkestra-resume sample-app", bound, "003-x"))
            self.assertFalse(off_topic_vs_project("onaylıyorum", bound, "003-x"))
            self.assertFalse(off_topic_vs_project(
                "demo kaydını düzelt", bound, "003-x", title="Demo Kayıt"))
            self.assertFalse(off_topic_vs_project("şimdi commit et", bound, "003-x"))
            self.assertFalse(off_topic_vs_project("testleri de çalıştır", bound, "003-x"))
        finally:
            os.environ.pop("ORKESTRA_WORKSPACE", None)


SIZE_CASES = {
    "typo düzelt README": "XS",
    "çok basit rename: Foo → Bar": "XS",
    "boyut: XS sadece bunu değiştir": "XS",
    "explain how the dashboard tracks runs": "XS",
    "bu gate nasıl çalışır?": "XS",
    "sub fonksiyonu ekle": "S",
    "küçük bugfix: null check": "S",
    "dark mode özelliği ekle": "M",
    "yeni ekran ve endpoint ekle": "M",
    "add oauth login": "M",
    "tüm uygulamayı migrate et": "L",
    "bulk multi-platform refactor": "L",
    "size: L rewrite the whole module": "L",
    "ödeme entegrasyonu ekle": "M",
    "giriş şifresi sıfırlama ekle": "M",
    "oauth nasıl çalışır?": "XS",
    "explain how payments work": "XS",
    "bu endpoint nasıl çalışır?": "XS",
    "boyut: S add oauth login": "M",
    "boyut: L typo düzelt README": "XS",
    "typo düzelt payment label": "XS",
    "add a login page": "M",
    "fix session expiry": "M",
    "rotate the jwt token": "M",
    "prevent csrf on forms": "M",
    "fix race condition in the queue": "M",
    "add rate limit to the api": "M",
    "add an index on users": "M",
    "export reports to csv in the admin": "M",
    "import contacts from the file into the app": "M",
    "upgrade dependency upgrade lodash": "M",
    "fix typo": "XS",
    "bugfix: null check": "S",
    # small local edits stay S (they were M once the length fallback went away)
    "change the button color to blue": "S", "bump version to 1.2.3": "S", "remove unused import": "S",
    "add logging to the worker": "S", "make header sticky": "S", "sort the list alphabetically": "S",
    "update the index page title": "S", "update the design token for primary color": "S",
    "buton rengini değiştir": "S", "index.html başlığını değiştir": "S", "README'ye kurulum notu ekle": "S",
    # the risky neighbours stay M
    "add an index on the users table": "M", "create index on orders": "M", "rotate the access token": "M",
    "add logging to the login flow": "M", "change the password reset title": "M",
}


class TestInferSize(unittest.TestCase):
    def test_cases(self):
        for text, want in SIZE_CASES.items():
            self.assertEqual(infer_size(text), want, text)

    def test_empty_defaults_m(self):
        self.assertEqual(infer_size(""), "M")

    def test_a_long_small_fix_stays_on_the_fast_track(self):
        text = "küçük bugfix: null check. " + ("Ayrıntı. " * 80)
        self.assertGreater(len(text), 480)
        self.assertEqual(infer_size(text), "S")

    def test_a_long_article_stays_on_the_fast_track(self):
        text = "kind: writing bir makale hazırla. " + ("Giriş ve kaynak. " * 40)
        self.assertGreater(len(text), 480)
        self.assertEqual(infer_size(text), "S")

    def test_a_long_feature_stays_on_the_full_pipeline(self):
        text = "yeni ekran ekle. " + ("Ayrıntı. " * 80)
        self.assertGreater(len(text), 480)
        self.assertEqual(infer_size(text), "M")

    def test_a_long_vague_request_stays_m(self):
        text = "bunu toparla lütfen. " + ("cümle. " * 80)
        self.assertGreater(len(text), 480)
        self.assertEqual(infer_size(text), "M")

    def test_slim_helpers(self):
        self.assertTrue(skips_plan_critic("XS") and skips_plan_critic("S"))
        self.assertFalse(skips_plan_critic("M"))
        self.assertTrue(skips_adversary("S") and not skips_adversary("L"))
        self.assertEqual(min_context_chars("XS"), 80)
        self.assertEqual(min_context_chars("S"), 200)

    def test_advisory_detection_and_title(self):
        self.assertTrue(is_advisory_request("explain how orkestra watch works"))
        self.assertTrue(is_advisory_request("bu dosya ne işe yarar?"))
        self.assertFalse(is_advisory_request("dark mode özelliği ekle"))
        self.assertFalse(is_advisory_request("add oauth login"))
        self.assertTrue(is_advisory_request("explain how payments work"))

    def test_a_flag_cannot_shrink_a_risk_or_inflate_a_typo(self):
        self.assertEqual(resolve_size("S", "add oauth login"), "M")
        self.assertEqual(resolve_size("L", "typo düzelt README"), "XS")
        self.assertEqual(resolve_size("S", "sub fonksiyonu ekle"), "S")
        self.assertEqual(resolve_size("XS", "sub fonksiyonu ekle"), "XS")
        self.assertEqual(resolve_size("M", "sub fonksiyonu ekle"), "M")
        self.assertEqual(resolve_size("L", "sub fonksiyonu ekle"), "L")
        self.assertEqual(resolve_size("auto", "küçük bugfix: null check"), "S")
        self.assertEqual(title_from_request("explain how the dashboard tracks open runs please"),
                         "explain how the dashboard tracks open")
        self.assertEqual(title_from_request(""), "request")


class TestEnglishApproval(unittest.TestCase):
    """A typed English approval is recorded only when it is a plain yes. A yes with a change, a condition or a
    promise attached is not an approval: the run would start implementing against a plan the user wants changed."""

    def test_approval_with_a_change_request_is_not_an_approval(self):
        for text in ("approve it but change the button color", "Approve the plan, but add error handling",
                     "approve this, but use postgres instead", "approve it. also change the title",
                     "I approve, but please change the color scheme", "approved but remove the CSV export",
                     "I approve this plan, but add logging first", "go ahead but use blue instead",
                     "approved, except the migration", "lgtm but drop the dark mode part",
                     "approve the plan but change the auth approach", "approve the spec but remove the migration step",
                     "approve the plan except for the migration", "approve the plan, skip T002",
                     "approve it, then rename the module", "I approve, can you also add a test for it",
                     "approve it and please drop T003"):
            with self.subTest(text=text):
                self.assertNotEqual(approval(text), "approve")

    def test_approval_with_a_change_request_is_recorded_as_a_change_request_where_it_names_one(self):
        for text in ("approve it but change the button color", "Approve the plan, but add error handling",
                     "approve it. also change the title", "approved, except the migration",
                     "go ahead but use blue instead", "lgtm but drop the dark mode part"):
            with self.subTest(text=text):
                self.assertEqual(approval(text), "reject")

    def test_conditions_and_promises_are_not_approvals(self):
        for text in ("approve this plan only if you drop T004", "approve the plan after you add tests",
                     "approve it once the tests pass", "I'll approve the plan tomorrow",
                     "I will approve the plan after lunch", "we'll approve it later", "approve it when the CI is green"):
            with self.subTest(text=text):
                self.assertNotEqual(approval(text), "approve")

    def test_a_negation_that_is_not_about_approving_does_not_void_the_approval(self):
        for text in ("Don't worry, I approve", "I can't see any issue, I approve", "Not a problem, I approve.",
                     "I do not object. I approve.", "Cannot wait, I approve", "I won't change anything, I approve",
                     "No problem at all, approved", "Nothing to add, I approve this plan",
                     "don’t worry, I approve", "I approve this plan, go ahead", "Plan approved, thanks",
                     "I approve. Implement it carefully without breaking anything and with thorough tests"):
            with self.subTest(text=text):
                self.assertEqual(approval(text), "approve")

    def test_a_negation_of_approving_still_voids_it(self):
        for text in ("I do not approve", "I don’t approve this", "I can't approve this yet", "never approve",
                     "I won't approve it", "I'm not going to approve this", "not approved", "Do not approve it"):
            with self.subTest(text=text):
                self.assertNotEqual(approval(text), "approve")

    def test_plain_english_yes_words_approve(self):
        for text in ("proceed", "Continue", "start", "ok", "Okay.", "yes please", "looks good", "Looks good to me!",
                     "accept", "do it", "ok, go ahead", "yes, proceed", "sounds good", "ship it", "go for it"):
            with self.subTest(text=text):
                self.assertEqual(approval(text), "approve")
        for text in ("ok wait", "ok but not yet", "start later", "continue? not sure", "okay, remove T002",
                     "proceed without the tests", "no", "wait"):
            with self.subTest(text=text):
                self.assertNotEqual(approval(text), "approve")

    def test_approval_answer_with_typed_text_after_the_option_label(self):
        for text in ("Approve and start implementing; also remove the CSV export",
                     "Approve and start implementing, drop T002", "Approve and start implementing, add a rollback task",
                     "Approve and start implementing, skip the migration", "Approve and start implementing except T003",
                     "Approve and start implementing without the tests", "Approve and start implementing, remove T004",
                     "Approve and start implementing, but drop T002", "Approve and start implementing later"):
            with self.subTest(text=text):
                self.assertEqual(approval_answer(text), "reject")
        for text in ("Approve and start implementing", "approve and start implementing.",
                     "Onayla — uygulamaya başla", "Approve and start implementing, thanks"):
            with self.subTest(text=text):
                self.assertEqual(approval_answer(text), "approve")

    def test_manual_approval_request_phrasings(self):
        for text in ("wait for my approval before implementing", "I want to review the plan first",
                     "let me review the plan before you start", "ask me before you start coding",
                     "require my approval", "use manual approval", "get my sign-off first",
                     "ask me for approval", "I'd like to review the plan", "switch to manual approval mode",
                     "Please wait for my go-ahead before you code"):
            with self.subTest(text=text):
                self.assertTrue(manual_approval_requested(text))
        for text in ("don't wait for my approval", "no need for my approval", "go without my approval",
                     "use autopilot, not manual approval", "I never want to review the plan",
                     "if the plan is risky, let me review the plan", "review the plan yourself",
                     "the review should cover the plan"):
            with self.subTest(text=text):
                self.assertFalse(manual_approval_requested(text))

    def test_a_typographic_apostrophe_reads_like_the_ascii_one(self):
        self.assertEqual(git_grants("don’t push it"), git_grants("don't push it"))
        self.assertNotIn("push", git_grants("don’t push it"))
        self.assertEqual(approval("I can’t approve this yet"), approval("I can't approve this yet"))


class TestManualApprovalScope(unittest.TestCase):
    """A negation only cancels a manual-approval request when it is about asking/approving in the same clause, and
    ordinary feature wording ("releases need my approval") is not a request to keep the plan decision."""

    def test_a_negation_elsewhere_in_the_message_does_not_drop_an_explicit_request(self):
        for text in ("Refactor the parser without breaking the API. Ask me for approval before you implement.",
                     "Refactor the parser without breaking the API. Let me approve the plan.",
                     "Add caching without changing the public interface; I want to decide the approach.",
                     "Refactor the parser without breaking the API, and ask me for approval before you start.",
                     "Skip the cache, no need for a rewrite. Ask me for approval before coding.",
                     "I want to approve the plan, not the manual one",
                     "Make onboarding not the manual process it is today. Ask me for approval before coding.",
                     "Don't touch the schema. Ask me for approval before coding.",
                     "- never edit the lockfile\n- ask me for approval before you implement"):
            with self.subTest(text=text):
                self.assertTrue(manual_approval_requested(text))

    def test_a_negation_that_governs_the_approval_still_cancels_it(self):
        for text in ("Go without asking me for approval", "no need for my approval, just do it",
                     "Work without my approval, ask me for approval only if the plan is risky",
                     "use autopilot, not manual approval", "autopilot please, not the manual approval mode",
                     "Don't ask me for approval. Let me know when it is done."):
            with self.subTest(text=text):
                self.assertFalse(manual_approval_requested(text))

    def test_ordinary_feature_wording_is_not_a_manual_approval_request(self):
        for text in ("Add a deploy page where releases need my approval before going live",
                     "The workflow should require my approval before sending any email",
                     "I want to see the design in dark mode, so make the dark theme",
                     "I want to check the design system docs, then add a tooltip component",
                     "I want to see the plan for the migration after the run",
                     "If I want to review the plan I will tell you",
                     "Add a setting to enable manual approval of refunds",
                     "Let me see the design in dark mode first, then add the toggle"):
            with self.subTest(text=text):
                self.assertFalse(manual_approval_requested(text))

    def test_the_imperative_forms_of_the_request_still_count(self):
        for text in ("Build the importer, then wait for my approval before implementing",
                     "Add the exporter. Require my approval first.",
                     "You must get my sign-off before you code", "The plan needs my approval before you implement",
                     "Please wait for my go-ahead before you code", "let me review the plan before you start",
                     "I want to review the spec first", "Refactor the cache, I'd like to review the plan.",
                     "enable manual approval for this run"):
            with self.subTest(text=text):
                self.assertTrue(manual_approval_requested(text))


class TestApprovalPhraseScope(unittest.TestCase):
    """`approve it` / `approve the plan` inside a longer message is not an approval of a waiting plan unless the message
    is a plain imperative; a condition, a limit or a retraction anywhere in the message voids an approval, in English
    and in Turkish."""

    def test_a_message_that_merely_contains_approve_it_is_not_an_approval(self):
        for text in ("Approve it? Not yet.", "Approve the plan? I am not sure yet", "approve the plan? what are the risks",
                     "Only the tech lead can approve this", "please approve this pull request on GitHub",
                     "Why approve it so quickly", "Can the CI approve the plan automatically",
                     "I'll let the lead approve this", "approve this pull request"):
            with self.subTest(text=text):
                self.assertNotEqual(approval(text), "approve")

    def test_a_plain_imperative_approval_still_approves(self):
        for text in ("approve it", "Approve the plan.", "approve it and go", "Approve the plan, then implement it carefully",
                     "approve this, thanks", "approve the spec and start"):
            with self.subTest(text=text):
                self.assertEqual(approval(text), "approve")

    def test_english_conditions_limits_and_retractions_void_the_approval(self):
        for text in ("I approve the plan, provided the migration is reversible",
                     "I approve this plan, assuming the tests pass", "I approve, as long as tests are green",
                     "I approve this, pending the security review", "I approve this subject to legal sign-off",
                     "Conditionally approved", "Plan approved on the condition that nothing is deployed",
                     "Approved for T001 only", "I approve the first two tasks", "I approve the plan without T003",
                     "I approve. Actually, wait.", "I approve the plan, once you rename the module",
                     "I approve, make T003 smaller", "I approve. Do not implement T003.",
                     "approved, but hold off on the migration for now", "I approve, not yet though"):
            with self.subTest(text=text):
                self.assertNotEqual(approval(text), "approve")

    def test_turkish_negations_conditions_and_questions_void_the_approval(self):
        for text in ("Planı onaylamadım", "Bu plan onaylanmadı", "planı onaylamadan başlama",
                     "planı onaylamak istemiyorum", "planı onaylayabilir miyim",
                     "Planı onaylamam için önce testleri göster", "planı onaylayacağım ama yarın",
                     "Bence plan iyi, onaylıyorum ama önce bir bakayım", "Onaylıyorum fakat başlama",
                     "onaylıyorum, planı T003 hariç", "onaylıyorum, yalnızca T001 olsun",
                     "planı onaylarım ancak testler geçerse"):
            with self.subTest(text=text):
                self.assertNotEqual(approval(text), "approve")

    def test_a_plain_statement_that_the_plan_was_not_approved_is_a_rejection(self):
        for text in ("Planı onaylamadım", "Bu plan onaylanmadı", "planı onaylamak istemiyorum"):
            with self.subTest(text=text):
                self.assertEqual(approval(text), "reject")

    def test_fluff_around_a_plain_approval_still_approves(self):
        for text in ("I approve. Implement it carefully and run all tests after each task.",
                     "I approve the plan, start with T001", "I approve this plan, go ahead and ship it",
                     "Looks good, I approve the plan. Thanks!", "I approve, and don't worry about the old tests",
                     "bu planı da onaylıyorum yap en uzman şekilde en kapsamlı testlerle hiçbir sağlıklı yapıyı bozmadan yap",
                     "planı onaylıyorum, testleri de çalıştır", "Onaylıyorum, devam et lütfen",
                     "Planı onaylıyorum, T001 ile başla", "Planı onaylıyorum, T001'den başlayalım"):
            with self.subTest(text=text):
                self.assertEqual(approval(text), "approve")



if __name__ == "__main__":
    unittest.main()
