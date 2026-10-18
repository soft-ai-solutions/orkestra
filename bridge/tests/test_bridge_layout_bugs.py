"""FR-004 / FR-006: narrow runs heading and dark text contrast, read from the UI sources."""
import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "ui"
CSS_PATH = UI / "app.css"
HTML_PATH = UI / "index.html"
I18N_PATH = UI / "i18n.js"

BRAND = ".brand-mark"
COMPLETE_DOT = ".flow-stage.is-complete .flow-stage-dot"


class Node:
    def __init__(self, tag, classes, node_id=None, attrs=None, parent=None):
        self.tag = tag
        self.classes = frozenset(classes)
        self.id = node_id
        self.attrs = attrs or {}
        self.parent = parent


def _strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def parse_rules(css):
    css = _strip_comments(css)
    rules = []
    order = 0

    def parse_list(i, media):
        nonlocal order
        n = len(css)
        while i < n:
            while i < n and css[i].isspace():
                i += 1
            if i >= n:
                return i
            if css[i] == "}":
                return i + 1
            if css.startswith("@media", i):
                j = css.find("{", i)
                cond = css[i + len("@media"):j].strip()
                i = parse_list(j + 1, media + (cond,))
                continue
            if css[i] == "@":
                j = css.find("{", i)
                if j < 0:
                    k = css.find(";", i)
                    return n if k < 0 else k + 1
                i = parse_list(j + 1, media)
                continue
            j = css.find("{", i)
            if j < 0:
                break
            selector = css[i:j].strip()
            end = css.find("}", j)
            body = css[j + 1:end]
            rules.append((media, selector, body, order))
            order += 1
            i = end + 1
        return i

    parse_list(0, ())
    return rules


def declarations(body):
    found = []
    for part in body.split(";"):
        if ":" not in part:
            continue
        name, value = part.split(":", 1)
        name = name.strip().lower()
        value = re.sub(r"\s+", " ", value.strip())
        value = re.sub(r"\s*!important\s*$", "", value, flags=re.I).strip()
        if name:
            found.append((name, value))
    return found


def split_selectors(selector):
    parts, buf, depth = [], [], 0
    for ch in selector:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def compounds(selector):
    tokens, buf, combinator, depth = [], [], None, 0
    i, s = 0, selector.strip()
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if depth == 0 and ch in ">+~":
            tokens.append((combinator, "".join(buf).strip()))
            buf, combinator = [], ch
            i += 1
            while i < len(s) and s[i].isspace():
                i += 1
            continue
        if depth == 0 and ch.isspace():
            j = i + 1
            while j < len(s) and s[j].isspace():
                j += 1
            if j < len(s) and s[j] in ">+~":
                i = j
                continue
            if "".join(buf).strip():
                tokens.append((combinator, "".join(buf).strip()))
                buf, combinator = [], " "
            i = j
            continue
        buf.append(ch)
        i += 1
    if "".join(buf).strip():
        tokens.append((combinator, "".join(buf).strip()))
    return [tok for tok in tokens if tok[1]]


def specificity(selector):
    ids = re.findall(r"#[\w-]+", selector)
    classes = re.findall(r"\.[\w-]+", selector)
    attrs = re.findall(r"\[[^\]]+\]", selector)
    pseudos = re.findall(r"(?<!:):(?!:)[\w-]+", selector)
    rest = re.sub(r"#[\w-]+", " ", selector)
    rest = re.sub(r"\.[\w-]+", " ", rest)
    rest = re.sub(r"\[[^\]]+\]", " ", rest)
    rest = re.sub(r"::[\w-]+(?:\([^)]*\))?", " ", rest)
    rest = re.sub(r":[\w-]+(?:\([^)]*\))?", " ", rest)
    rest = re.sub(r"[>+~*]", " ", rest)
    elements = re.findall(r"[\w-]+", rest)
    return (len(ids), len(classes) + len(attrs) + len(pseudos), len(elements))


def _compound(compound, node):
    if "::" in compound:
        return False
    text = compound.strip()
    if text == ":root":
        return node.tag == ":root"
    if re.search(r":(?!:)", text):
        return False
    ids = re.findall(r"#([\w-]+)", text)
    if ids and (len(ids) != 1 or ids[0] != node.id):
        return False
    classes = re.findall(r"\.([\w-]+)", text)
    if not set(classes) <= node.classes:
        return False
    for name, op, val in re.findall(r'\[([\w-]+)(?:([~|^$*]?=)"?([^"\]]*)"?)?\]', text):
        have = node.attrs.get(name)
        if have is None or (op and op != "=") or (op == "=" and have != val):
            return False
    rest = re.sub(r"\[[^\]]+\]", "", text)
    rest = re.sub(r"#[\w-]+", "", rest)
    rest = re.sub(r"\.[\w-]+", "", rest).strip()
    if rest in ("", "*"):
        return True
    return rest == node.tag


def matches(selector, node):
    tokens = compounds(selector)

    def walk(parts, current):
        if not parts or not _compound(parts[-1][1], current):
            return False
        if len(parts) == 1:
            return True
        comb = parts[-1][0]
        prev = parts[:-1]
        if comb == " ":
            parent = current.parent
            while parent is not None:
                if walk(prev, parent):
                    return True
                parent = parent.parent
            return False
        if comb == ">":
            return current.parent is not None and walk(prev, current.parent)
        return False

    return walk(tokens, node)


def media_ok(chain, flags):
    for cond in chain:
        text = " ".join(cond.lower().split())
        if "prefers-color-scheme" in text:
            if bool(flags.get("dark")) != ("dark" in text):
                return False
            continue
        if "prefers-reduced-motion" in text:
            return False
        width = flags.get("width")
        max_w = re.search(r"max-width:\s*(\d+)px", text)
        if max_w:
            if width is None or width > int(max_w.group(1)):
                return False
            continue
        min_w = re.search(r"min-width:\s*(\d+)px", text)
        if min_w:
            if width is None or width < int(min_w.group(1)):
                return False
            continue
        return False
    return True


def winning(rules, node, prop, flags):
    best = None
    for media, selector, body, order in rules:
        if not media_ok(media, flags):
            continue
        for sel in split_selectors(selector):
            if not matches(sel, node):
                continue
            spec = specificity(sel)
            for name, value in declarations(body):
                if name != prop:
                    continue
                rank = (spec, order)
                if best is None or rank >= best[0]:
                    best = (rank, value, media, order)
    return best


def custom_properties(rules, flags):
    root = Node(":root", [])
    values, best = {}, {}
    for media, selector, body, order in rules:
        if not media_ok(media, flags):
            continue
        for sel in split_selectors(selector):
            if not matches(sel, root):
                continue
            spec = specificity(sel)
            for name, value in declarations(body):
                if not name.startswith("--"):
                    continue
                rank = (spec, order)
                if name not in best or rank >= best[name]:
                    best[name] = rank
                    values[name] = value
    return values


def early_dark_rules(rules):
    start = None
    chain = None
    for i, (media, _sel, _body, _order) in enumerate(rules):
        if any("prefers-color-scheme" in cond and "dark" in cond for cond in media):
            start, chain = i, media
            break
    if start is None:
        return []
    found = []
    for rule in rules[start:]:
        if rule[0] != chain:
            break
        found.append(rule)
    return found


def resolve_color(value, variables):
    seen = set()
    current = value.strip()
    while True:
        match = re.fullmatch(r"var\(\s*(--[\w-]+)\s*\)", current)
        if not match:
            return current
        name = match.group(1)
        if name in seen or name not in variables:
            raise AssertionError(f"unresolved {current}")
        seen.add(name)
        current = variables[name].strip()


def rgb(value):
    text = value.strip().lower()
    if text == "white":
        return (255, 255, 255)
    if text == "black":
        return (0, 0, 0)
    if re.fullmatch(r"#[0-9a-f]{3}", text):
        return tuple(int(ch * 2, 16) for ch in text[1:])
    if re.fullmatch(r"#[0-9a-f]{6}", text):
        return tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))
    raise AssertionError(f"not a color: {value!r}")


def _channel(c):
    # c is the sRGB channel in 0..1
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def contrast_ratio(fg, bg):
    def lum(color):
        r, g, b = (channel / 255 for channel in color)
        return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)

    hi, lo = lum(fg), lum(bg)
    if hi < lo:
        hi, lo = lo, hi
    return (hi + 0.05) / (lo + 0.05)


def message(lang, key):
    js = I18N_PATH.read_text(encoding="utf-8")
    en, tr = js.split("\n    tr: {\n", 1)
    block = en if lang == "en" else tr
    match = re.search(rf'"{re.escape(key)}": "((?:\\.|[^"\\])*)"', block)
    if not match:
        raise AssertionError(f"missing {lang} {key}")
    return match.group(1)


def element_by_id(html, node_id):
    match = re.search(rf'<(\w+)([^>]*\bid="{re.escape(node_id)}"[^>]*)>', html)
    if not match:
        raise AssertionError(f"missing #{node_id}")
    attrs = match.group(2)
    class_match = re.search(r'class="([^"]*)"', attrs)
    classes = class_match.group(1).split() if class_match else []
    return Node(match.group(1), classes, node_id)


def is_max_520(media):
    return any(re.search(r"max-width:\s*520px", cond) for cond in media)


class BridgeLayoutBugsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = CSS_PATH.read_text(encoding="utf-8")
        cls.html = HTML_PATH.read_text(encoding="utf-8")
        cls.rules = parse_rules(cls.css)
        cls.shell = re.search(r'<main\s+class="shell">(.*)</main>', cls.html, re.S).group(1)

    def test_narrow_hides_jobs_updated_and_keeps_refresh(self):
        updated = element_by_id(self.html, "jobs-updated")
        self.assertNotIn("jobs-updated", updated.classes)
        self.assertIn("field-hint", updated.classes)
        hidden = winning(self.rules, updated, "display", {"width": 520})
        self.assertIsNotNone(
            hidden,
            "#jobs-updated stays visible: the 520px rule targets .jobs-updated, which this node does not have",
        )
        self.assertEqual(hidden[1].lower(), "none")
        self.assertTrue(is_max_520(hidden[2]), hidden[2])
        refresh = element_by_id(self.html, "refresh-button")
        shown = winning(self.rules, refresh, "display", {"width": 520})
        self.assertTrue(shown is None or shown[1].lower() != "none")
        self.assertIn('id="refresh-button"', self.shell)
        self.assertTrue(message("en", "jobs.refresh").strip())
        self.assertTrue(message("tr", "jobs.refresh").strip())
        self.assertIsNone(re.search(r"direction\s*:\s*rtl", self.css, re.I))

    def test_narrow_heading_wraps_inside_the_shell(self):
        self.assertRegex(self.shell, r'<h2[^>]*\bdata-i18n="jobs.heading"[^>]*>')
        self.assertEqual(message("en", "jobs.heading"), "Runs")
        self.assertEqual(message("tr", "jobs.heading"), "Çalışmalar")
        heading = Node("div", ["jobs-heading"])
        wrapped = winning(self.rules, heading, "flex-wrap", {"width": 520})
        self.assertIsNotNone(wrapped, ".jobs-heading does not wrap at 520px")
        self.assertEqual(wrapped[1].lower(), "wrap")
        self.assertTrue(is_max_520(wrapped[2]), wrapped[2])

    def test_dark_text_contrasts_and_light_pair_stays(self):
        early = early_dark_rules(self.rules)
        self.assertTrue(early, "missing dark media")
        for _media, selector, body, _order in early:
            self.assertEqual(selector.strip(), ":root")
            for name, value in declarations(body):
                # color-scheme is the existing scheme switch; a text color here would lose to the later white rules.
                self.assertTrue(
                    name.startswith("--") or name == "color-scheme",
                    f"early dark block is not variables only: {name}",
                )
                self.assertNotEqual(name, "color")
                if name == "--accent":
                    self.assertEqual(value.lower(), "#79cbb1")
        light_vars = custom_properties(self.rules, {"dark": False})
        dark_vars = custom_properties(self.rules, {"dark": True})
        self.assertEqual(light_vars["--accent"].lower(), "#216b58")
        self.assertEqual(dark_vars["--accent"].lower(), "#79cbb1")
        brand = Node("span", ["brand-mark"])
        stage = Node("div", ["flow-stage", "is-complete"])
        dot = Node("span", ["flow-stage-dot"], parent=stage)
        light_brand = winning(self.rules, brand, "color", {"dark": False})
        light_dot = winning(self.rules, dot, "color", {"dark": False})
        self.assertEqual(light_brand[1].lower(), "#fff")
        self.assertEqual(light_dot[1].lower(), "white")
        anchor = winning(self.rules, dot, "color", {"dark": False})
        for node in (brand, dot):
            color = winning(self.rules, node, "color", {"dark": True})
            background = winning(self.rules, node, "background", {"dark": True})
            self.assertIsNotNone(color)
            self.assertIsNotNone(background)
            fg = rgb(resolve_color(color[1], dark_vars))
            bg = rgb(resolve_color(background[1], dark_vars))
            self.assertEqual(bg, rgb("#79cbb1"))
            ratio = contrast_ratio(fg, bg)
            self.assertGreaterEqual(ratio, 4.5, f"{color[1]} on {background[1]} is {ratio:.2f}:1")
            self.assertGreater(color[3], anchor[3], "dark text color must follow the white complete-dot rule")
            self.assertTrue(
                any("prefers-color-scheme" in cond and "dark" in cond for cond in color[2]),
                color[2],
            )
        light_bg = winning(self.rules, brand, "background", {"dark": False})
        light_ratio = contrast_ratio(
            rgb(resolve_color(light_brand[1], light_vars)),
            rgb(resolve_color(light_bg[1], light_vars)),
        )
        self.assertGreaterEqual(light_ratio, 4.5)


if __name__ == "__main__":
    unittest.main()
