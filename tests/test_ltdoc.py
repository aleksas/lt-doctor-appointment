"""Offline tests for name matching and review analysis. Run: python3 -m unittest discover tests"""
import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import ltdoc  # noqa: E402

TODAY = dt.date(2026, 11, 26)


def review(date, sentiment="positive", text="Gydytoja išklausė, paaiškino ir paskyrė tinkamą gydymą.",
           author="registered", count=3):
    return {"source": "test", "date": date, "sentiment": sentiment, "author": author,
            "author_review_count": count, "text": text}


class NameMatching(unittest.TestCase):
    def test_exact_ignores_order_case_and_diacritics(self):
        self.assertEqual(ltdoc.match_score("JŪRATĖ GARBARAVIČIENĖ", "Garbaravicienė Jūratė"), 3)

    def test_middle_name_is_partial(self):
        self.assertEqual(ltdoc.match_score("Audronė Diana Montvilienė", "Montvilienė Audronė"), 2)

    def test_double_surname(self):
        self.assertEqual(ltdoc.match_score("Gabija Rudzikaitė", "Rudzikaitė-Fergizė Gabija"), 2)

    def test_different_first_name_is_no_match(self):
        self.assertEqual(ltdoc.match_score("Vilma Markevičienė", "Rasa Markevičienė"), 0)

    def test_clinic_tokens_strip_quotes(self):
        self.assertIn("narmedos", ltdoc.name_tokens('UAB "NARMEDOS MEDICINA"'))

    def test_clinic_stems_tolerate_endings(self):
        self.assertTrue(ltdoc.stems_overlap({"narmedos"}, {"narmeda"}))
        self.assertFalse(ltdoc.stems_overlap({"antakalnio"}, {"seskines"}))


BUNDLE = (
    'apiUrl:"/api",' + ",".join(f'{k}:"/x/{k}"' for k in ltdoc.IPR_PATH_KEYS) +
    ';var cr=(()=>(function(n){n[n.All=0]="All",n[n.REQUIRED=1]="REQUIRED",n[n.NOT_REQUIRED=2]="NOT_REQUIRED"}'
    '(cr||(cr={})),cr))();new Rl(cr.All,"Visi"),new Rl(cr.REQUIRED,"Su siuntimu"),'
    'new Rl(cr.NOT_REQUIRED,"Be siuntimo");'
    'var lr=(()=>(function(n){n[n.All=0]="All",n[n.VLK=1]="VLK",n[n.Patient=2]="Patient"}(lr||(lr={})),lr))();'
    'new Rl(lr.All,"Visi"),new Rl(lr.VLK,"Ligoni\\u0173 kasos"),new Rl(lr.Patient,"Pacientas");'
    '(e.referralNeedType||e.referralNeedType===cr.All);(e.paymentType||e.paymentType===lr.All);'
    + "".join(f'"{p}"' for p in ltdoc.IPR_PARAMS)
)


class Discovery(unittest.TestCase):
    def test_parse_bundle(self):
        cfg = ltdoc.parse_ipr_bundle(BUNDLE)
        self.assertEqual(cfg["apiUrl"], "/api")
        self.assertEqual(cfg["paths"]["appointmentTimeGroupsSearchPath"], "/x/appointmentTimeGroupsSearchPath")
        ref = {o["member"]: (o["value"], o["label"]) for o in cfg["filters"]["referralNeedType"]}
        self.assertEqual(ref["NOT_REQUIRED"], (2, "Be siuntimo"))
        pay = {o["member"]: o["label"] for o in cfg["filters"]["paymentType"]}
        self.assertEqual(pay["VLK"], "Ligonių kasos")

    def test_parse_bundle_missing_path_fails(self):
        with self.assertRaises(ValueError):
            ltdoc.parse_ipr_bundle(BUNDLE.replace("appointmentTimeGroupsSearchPath", "renamed"))

    def test_resolve_filter_by_member_label_or_value(self):
        ipr = object.__new__(ltdoc.Ipr)
        ipr.cfg = ltdoc.parse_ipr_bundle(BUNDLE)
        for q in ("NOT_REQUIRED", "not-required", "Be siuntimo", "2"):
            self.assertEqual(ipr.resolve_filter("referralNeedType", q)["value"], 2, q)
        self.assertEqual(ipr.resolve_filter("paymentType", "ligoniu")["member"], "VLK")
        self.assertIsNone(ipr.resolve_filter("paymentType", None))
        with self.assertRaises(SystemExit):
            ipr.resolve_filter("paymentType", "bitcoin")

    def test_specialty_hint_from_profession_name(self):
        self.assertEqual(ltdoc.specialty_hint("Gydytojas dermatovenerologas"), "derma")
        self.assertEqual(ltdoc.specialty_hint("Gydytojas akušeris ginekologas"), "ginek")


class ReviewAnalysis(unittest.TestCase):
    def test_recent_reviews_outweigh_old_ones(self):
        rs = [review("2012-01-01", "negative") for _ in range(10)] + \
             [review("2026-06-01", "positive") for _ in range(3)]
        a = ltdoc.analyze(rs, TODAY)
        self.assertLess(a["positive_pct_all_time"], 30)
        self.assertGreater(a["weighted_positive_pct"], 60)
        self.assertEqual((a["recent"]["n"], a["recent"]["positive_pct"]), (3, 100))

    def test_burst_is_flagged_and_downweighted(self):
        rs = [review(f"2026-03-0{d}", text=f"Puiki gydytoja, labai rekomenduoju visiems {d}") for d in (1, 2, 3)]
        rs.append(review("2026-05-01", "negative"))
        ltdoc.analyze(rs, TODAY)
        self.assertTrue(all("burst" in r["flags"] for r in rs[:3]))
        self.assertNotIn("burst", rs[3]["flags"])
        self.assertLess(rs[0]["trust"], rs[3]["trust"])

    def test_near_duplicates(self):
        t = "Labai profesionali gydytoja, viską paaiškino ir padėjo, rekomenduoju!"
        rs = [review("2025-01-01", text=t), review("2025-06-01", text=t + " ")]
        ltdoc.analyze(rs, TODAY)
        self.assertIn("near-duplicate", rs[0]["flags"])

    def test_site_hidden_negative_text_is_not_a_duplicate(self):
        hidden = "Neigiamas atsiliepimas parašytas prieš 3 m. 5 mėn., todėl nebeaktualus ir nerodomas."
        rs = [review("2023-05-01", "negative", hidden), review("2023-06-01", "negative", hidden)]
        a = ltdoc.analyze(rs, TODAY)
        self.assertEqual(a["negative_text_hidden_by_site"], 2)
        self.assertEqual(a["flagged_count"], 0)
        self.assertTrue(all(r["trust"] == 1.0 for r in rs))

    def test_anonymous_short_shouting(self):
        rs = [review("2026-01-01", "negative", "BAISI GYDYTOJA, NIEKAM NEREKOMENDUOJU!!!!", author="anonymous")]
        ltdoc.analyze(rs, TODAY)
        self.assertTrue({"anonymous", "shouting", "many exclamations"} <= set(rs[0]["flags"]))
        self.assertLess(rs[0]["trust"], 0.5)

    def test_neutral_reviews_are_not_counted_as_votes(self):
        rs = [review("2026-01-01", "neutral"), review("2026-02-01", "positive")]
        a = ltdoc.analyze(rs, TODAY)
        self.assertEqual((a["rated"], a["neutral"], a["positive_pct_all_time"]), (1, 1, 100))

    def test_no_reviews(self):
        a = ltdoc.analyze([], TODAY)
        self.assertIsNone(a["weighted_positive_pct"])
        self.assertEqual(a["effective_n"], 0)


if __name__ == "__main__":
    unittest.main()
