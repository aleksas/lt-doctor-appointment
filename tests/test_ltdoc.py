"""Offline tests. Run: python3 -m unittest discover tests"""
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


# A minimal stand-in for IPR's minified Angular bundle, in the same shapes.
BUNDLE = (
    'apiUrl:"/api",' + ",".join(f'{k}:"/x/{k}"' for k in ltdoc.IPR_PATH_KEYS) + ";"
    'var cr=(()=>(function(n){n[n.All=0]="All",n[n.REQUIRED=1]="REQUIRED",n[n.NOT_REQUIRED=2]="NOT_REQUIRED"}'
    '(cr||(cr={})),cr))();'
    'var lr=(()=>(function(n){n[n.All=0]="All",n[n.VLK=1]="VLK",n[n.Patient=2]="Patient"}(lr||(lr={})),lr))();'
    'var Gt=(()=>{return(n=Gt||(Gt={}))[n.ALL_REMOTE=-1]="ALL_REMOTE",n[n.INSTITUTION=1]="INSTITUTION",'
    'n[n.REMOTE_APPOINTMENT_VIDEO=3]="REMOTE_APPOINTMENT_VIDEO",Gt;var n})();'
    'f=[{label:"Apmok\\u0117jimas:",key:"paymentType",options:[{name:"Pacientas",value:lr.Patient},'
    '{name:"Ligoni\\u0173 kasos",value:lr.VLK}],visible:!0},'
    '{label:"Siuntimas:",key:"referralNeedType",options:[{name:"Su siuntimu",value:cr.REQUIRED},'
    '{name:"Be siuntimo",value:cr.NOT_REQUIRED}],visible:!0},'
    '{label:"Vizito b\\u016bdas:",key:"appointmentMethodId",options:[{name:"\\u012estaigoje",value:Gt.INSTITUTION},'
    '{name:"Nuotolinis: vaizdo skambutis",value:Gt.REMOTE_APPOINTMENT_VIDEO}],visible:!0}];'
    + "".join(f'"{p}"' for p in ltdoc.IPR_PARAMS)
)


class Discovery(unittest.TestCase):
    def test_parse_bundle_paths_and_all_form_filters(self):
        cfg = ltdoc.parse_ipr_bundle(BUNDLE)
        self.assertEqual(cfg["apiUrl"], "/api")
        self.assertEqual(cfg["paths"]["publicDates"], "/x/publicDates")
        self.assertEqual(set(cfg["filters"]), {"paymentType", "referralNeedType", "appointmentMethodId"})
        ref = {o["member"]: (o["value"], o["label"]) for o in cfg["filters"]["referralNeedType"]["options"]}
        self.assertEqual(ref["NOT_REQUIRED"], (2, "Be siuntimo"))
        pay = {o["member"]: o["label"] for o in cfg["filters"]["paymentType"]["options"]}
        self.assertEqual(pay["VLK"], "Ligonių kasos")
        self.assertEqual(cfg["filters"]["appointmentMethodId"]["label"], "Vizito būdas:")

    def test_missing_path_fails_loudly(self):
        with self.assertRaises(ValueError):
            ltdoc.parse_ipr_bundle(BUNDLE.replace("publicDates", "renamed"))

    def test_resolve_filter_by_member_label_or_value(self):
        ipr = object.__new__(ltdoc.Ipr)
        ipr.cfg = ltdoc.parse_ipr_bundle(BUNDLE)
        for q in ("NOT_REQUIRED", "not-required", "Be siuntimo", "2"):
            self.assertEqual(ipr.resolve_filter("referralNeedType", q)["value"], 2, q)
        self.assertEqual(ipr.resolve_filter("paymentType", "ligoniu")["member"], "VLK")
        self.assertEqual(ipr.resolve_filter("appointmentMethodId", "vaizdo")["value"], 3)
        self.assertIsNone(ipr.resolve_filter("paymentType", None))
        with self.assertRaises(SystemExit):
            ipr.resolve_filter("paymentType", "bitcoin")
        with self.assertRaises(SystemExit):
            ipr.resolve_filter("noSuchFilter", "x")

    def test_specialty_hint_from_profession_name(self):
        self.assertEqual(ltdoc.specialty_hint("Gydytojas dermatovenerologas"), "derma")
        self.assertEqual(ltdoc.specialty_hint("Gydytojas akušeris ginekologas"), "ginek")


class NameMatching(unittest.TestCase):
    def test_exact_ignores_order_case_and_diacritics(self):
        self.assertEqual(ltdoc.match_score("JŪRATĖ GARBARAVIČIENĖ", "Garbaravicienė Jūratė"), 3)

    def test_middle_name_is_partial(self):
        self.assertEqual(ltdoc.match_score("Audronė Diana Montvilienė", "Montvilienė Audronė"), 2)

    def test_double_surname(self):
        self.assertEqual(ltdoc.match_score("Gabija Rudzikaitė", "Rudzikaitė-Fergizė Gabija"), 2)

    def test_different_first_name_is_no_match(self):
        self.assertEqual(ltdoc.match_score("Vilma Markevičienė", "Rasa Markevičienė"), 0)

    def test_clinic_tokens_and_stems(self):
        self.assertIn("narmedos", ltdoc.name_tokens('UAB "NARMEDOS MEDICINA"'))
        self.assertTrue(ltdoc.stems_overlap({"narmedos"}, {"narmeda"}))
        self.assertFalse(ltdoc.stems_overlap({"antakalnio"}, {"seskines"}))


PINCETAS_HTML = """
<div class="panel-license"><p class="name"> Gydytojas dermatovenerologas </p>
<p class="info">Išduota: 2004-12-16, Nr. MPL-04813, <span class="status" style="color: green; ">Galiojanti</span>
(duomenys atnaujinti: 2020-09-22) </p></div>
<div class="panel-body rating-item doctors-at-hospital"> <div class="row">
<div class="col-sm-8" itemprop="hospitalAffiliation" itemscope> 1. <a href="#"><span itemprop="name">Narmeda</span></a> </div>
<div class="col-sm-4 quick-rating"> <a class="pull-right"> <span class="glyphicon glyphicon-comment" aria-hidden="true"></span> 21 </a>
<a class="pull-right"><span class="glyphicon glyphicon-heart"></span> 79%</a> </div> </div> </div>
<div class="rating-progress question-0"> <p>Ar skyrė pakankamai laiko?</p>
<div class="progress-wrapper"><div class="progress"></div></div><div class="compare"> Nėra įvertinimų </div></div>
<div class="rating-progress question-1"> <p>Kiek laiko laukėte, kol Jus priims?</p>
<div class="progress-wrapper"><div class="progress"></div><div class="progress-badge" style="left: 1px;">4.2</div></div>
<p class="compare">Priėmė iškarto sutartu laiku</p> </div>
<div  class="comment clearfix anonymous negative"> <div class="info"> 2016-05-01, 10:00 | IP: 10.0.0.1 </div>
<div class="message" style="x"><p>Neigiamas atsiliepimas parašytas prieš 9 m., todėl nebeaktualus ir nerodomas.</p></div></div>
"""

MANODAKTARAS_HTML = """
<script type="application/ld+json">{"@graph":[
 {"@type":"Person","makesOffer":[
  {"@type":"Offer","itemOffered":{"name":"Konsultacija (20 min.)"},"availableAtOrFrom":{"@id":"c1"},"price":"70.00","priceCurrency":"EUR"},
  {"@type":"Offer","name":"Laisvi","description":"Laisvi vizito laikai 2026-12-15 (Klinika X): 15:20, 15:40. Duomenys atnaujinti 2026-11-26 16:38.",
   "availableAtOrFrom":{"@id":"c1"},"availabilityStarts":"2026-12-15T15:20:00+02:00"}]},
 {"@type":"MedicalClinic","@id":"c1","name":"Klinika X"}]}</script>
<strong>100%</strong> (7 įvertinimai )
<div class="entry sm"><h4>Apie specialistą:</h4><p>Apgamų dermatoskopija<br />Aknės gydymas</p>
<p><strong>• Licencijos:</strong><br>MPL-1 Gydytojas<br><br></p>
<strong>• Kalba šiomis kalbomis:</strong><br>Lietuvių<br>Anglų<br><br><a class="btn">Palikti atsiliepimą</a></div>
<div class="col-md-6" data-comment-item><div class="comment-item"><div class="author"><div class="text"><h4></h4>
<div class="meta"> 2026-06-10 | Rekomenduoja </div></div></div><p> Labai gera gydytoja, viską paaiškino. </p></div></div>
"""


class Parsers(unittest.TestCase):
    def test_pincetas_extras(self):
        p = ltdoc.parse_pincetas_profile(PINCETAS_HTML)
        self.assertEqual(p["licences"][0]["issued"], "2004-12-16")
        self.assertEqual(p["licences"][0]["status"], "Galiojanti")
        self.assertEqual(p["workplace_stats"][0], {"name": "Narmeda", "reviews": 21, "recommend_pct": 79})
        self.assertIsNone(p["detailed_ratings"][0]["score"])  # unrated question doesn't borrow the next score
        self.assertEqual(p["detailed_ratings"][1]["score"], 4.2)
        r = p["reviews"][0]
        self.assertEqual((r["sentiment"], r["author"]), ("negative", "anonymous"))
        self.assertNotIn("10.0.0.1", str(p))  # reviewer IPs never kept

    def test_manodaktaras_extras(self):
        m = ltdoc.parse_manodaktaras_profile(MANODAKTARAS_HTML)
        self.assertEqual((m["site_recommend_pct"], m["site_ratings"]), (100.0, 7))
        self.assertEqual(m["prices"][0]["price"], "70.00")
        self.assertEqual(m["prices"][0]["clinic"], "Klinika X")
        self.assertEqual(m["free_times"][0], {"date": "2026-12-15", "clinic": "Klinika X", "times": ["15:20", "15:40"]})
        self.assertEqual(m["about"]["focus"], ["Apgamų dermatoskopija", "Aknės gydymas"])
        self.assertEqual(m["about"]["sections"]["Kalba šiomis kalbomis"], ["Lietuvių", "Anglų"])
        self.assertEqual(m["reviews"][0]["sentiment"], "positive")


class ReviewAnalysis(unittest.TestCase):
    def test_recent_reviews_outweigh_old_ones(self):
        rs = [review("2012-01-01", "negative") for _ in range(10)] + [review("2026-06-01") for _ in range(3)]
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

    def test_neutral_reviews_are_not_votes(self):
        a = ltdoc.analyze([review("2026-01-01", "neutral"), review("2026-02-01")], TODAY)
        self.assertEqual((a["rated"], a["neutral"], a["positive_pct_all_time"]), (1, 1, 100))

    def test_no_reviews(self):
        a = ltdoc.analyze([], TODAY)
        self.assertIsNone(a["weighted_positive_pct"])
        self.assertEqual(a["effective_n"], 0)


if __name__ == "__main__":
    unittest.main()
