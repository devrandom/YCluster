"""Static checks for the locally vendored Sigma rules and suppressions.

RSigma applies a suppression rule (``filter:`` naming the target rule ids in
``rules:``) silently: a suppression that names an id which no longer exists,
uses a field the normalizer never emits, or carries a logsource that does not
match the target loads without an error and simply never applies. A
re-vendored upstream rule can therefore turn a suppression into a no-op, and
the only visible symptom is the noise coming back. These tests fail at vendor
time instead.

They are static: a behavioural check needs the ``rsigma`` binary, which is
built on the cluster rather than here.
"""

import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only without PyYAML
    yaml = None

ROOT = Path(__file__).resolve().parents[4]
RULES_DIR = ROOT / "config/ansible/monitoring/files/rsigma/rules"
NORMALIZE_GO = ROOT / "ids-forwarder/sigma/normalize.go"
SUPPRESSION_GLOB = "zz-local-suppression*.yml"
# Modifiers the local suppressions are allowed to use. Kept small on purpose:
# an unknown modifier is another way for a suppression to become a no-op.
MODIFIERS = {"endswith", "startswith", "contains", "re", "all", "cidr"}


def load(path):
    with path.open() as fh:
        return yaml.safe_load(fh)


def normalized_fields():
    """Field names of the normalized event, not the raw Tetragon input.

    The tags are read up to the raw-input envelope (marked by its ``time``
    field), so a suppression cannot pass by naming an input field like ``pid``
    that the filter would never see.
    """
    lines = []
    for line in NORMALIZE_GO.read_text().splitlines():
        if 'json:"time"' in line:
            break
        lines.append(line)
    return set(re.findall(r'json:"([A-Za-z]+)[,"]', "\n".join(lines)))


def selections(doc):
    body = doc.get("filter", {})
    return {k: v for k, v in body.items() if k not in ("rules", "condition")}


@unittest.skipIf(yaml is None, "PyYAML is not available")
class RsigmaSuppressionTests(unittest.TestCase):
    def setUp(self):
        self.rules = {}
        for path in sorted(RULES_DIR.glob("*.yml")):
            doc = load(path)
            self.rules[doc["id"]] = (path.name, doc)
        self.suppressions = {
            p.name: load(p) for p in sorted(RULES_DIR.glob(SUPPRESSION_GLOB))
        }
        self.fields = normalized_fields()

    def test_curated_rules_have_unique_ids(self):
        seen = {}
        for path in sorted(RULES_DIR.glob("*.yml")):
            doc = load(path)
            self.assertIn("id", doc, path.name)
            self.assertNotIn(doc["id"], seen, "duplicate id in %s and %s" % (
                seen.get(doc["id"]), path.name))
            seen[doc["id"]] = path.name

    def test_normalizer_field_set_is_not_empty(self):
        # Guards the field check below against silently checking nothing.
        self.assertIn("CommandLine", self.fields)
        self.assertIn("ParentImage", self.fields)

    def test_there_are_suppressions_to_check(self):
        self.assertTrue(self.suppressions, "no local suppression rules found")

    def test_suppression_targets_exist(self):
        for name, doc in self.suppressions.items():
            targets = doc.get("filter", {}).get("rules", [])
            self.assertTrue(targets, "%s has no filter.rules" % name)
            for target in targets:
                self.assertTrue(target in self.rules,
                                "%s suppresses unknown rule id %s" % (name, target))

    def test_suppression_logsource_matches_target(self):
        # RSigma evaluates a suppression inside its own logsource. Compare the
        # keys that select it, not the whole dict, so an upstream rule adding
        # an unrelated key does not fail a suppression that still applies.
        for name, doc in self.suppressions.items():
            logsource = doc.get("logsource", {})
            for target in doc.get("filter", {}).get("rules", []):
                if target not in self.rules:
                    continue  # test_suppression_targets_exist reports it
                target_ls = self.rules[target][1].get("logsource", {})
                for key in ("product", "category"):
                    if key in logsource:
                        self.assertEqual(
                            logsource[key], target_ls.get(key),
                            "%s logsource %s does not match %s"
                            % (name, key, self.rules[target][0]))

    def test_suppression_fields_are_emitted_by_the_normalizer(self):
        # A field the normalizer never emits, or a misspelled one, makes the
        # suppression match nothing and fail silently at runtime.
        for name, doc in self.suppressions.items():
            sel = selections(doc)
            self.assertTrue(sel, "%s defines no filter selections" % name)
            for sel_name, body in sel.items():
                self.assertIsInstance(body, dict,
                                      "%s.%s is not a field mapping" % (name, sel_name))
                self.assertTrue(body, "%s.%s is empty (suppresses nothing or everything)"
                                % (name, sel_name))
                for key, value in body.items():
                    field, _, modifier = key.partition("|")
                    self.assertIn(field, self.fields,
                                  "%s.%s uses field %r the normalizer never emits"
                                  % (name, sel_name, field))
                    if modifier:
                        self.assertIn(modifier, MODIFIERS,
                                      "%s.%s uses unknown modifier %r" % (name, sel_name, modifier))
                    self.assertTrue(value not in (None, "", [], {}),
                                    "%s.%s.%s has no match value" % (name, sel_name, key))

    def test_suppression_condition_negates_a_named_filter(self):
        # The pattern in use is `condition: not <selection>`; a condition that
        # drops the negation would suppress everything the target matches.
        for name, doc in self.suppressions.items():
            condition = doc.get("filter", {}).get("condition", "")
            self.assertTrue(condition.startswith("not "),
                            "%s condition %r does not negate its filter" % (name, condition))
            referenced = condition[len("not "):].strip().split()
            self.assertTrue(referenced, "%s condition names nothing" % name)
            for ref in referenced:
                self.assertIn(ref, selections(doc),
                              "%s condition %r names no defined selection" % (name, ref))


if __name__ == "__main__":
    unittest.main()
