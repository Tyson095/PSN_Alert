"""Tests for the shared monitoring core.

Covers the four areas that matter for correctness in GitHub Actions:

* state persistence across separate processes,
* stock transitions (including the "unknown is never out of stock" rule),
* price-drop detection,
* duplicate-alert prevention.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from psn_gift_card_alert_bot import core

TZ = ZoneInfo("Asia/Kolkata")
T0 = "2026-11-08T09:00:00+05:30"
T1 = "2026-11-08T09:30:00+05:30"
T2 = "2026-11-08T10:00:00+05:30"


def product(**overrides):
    base = {
        "id": "rooter-psn-1000",
        "retailer": "Rooter",
        "denomination": 1000,
        "url": "https://shop.rooter.gg/product/playstation-gift-cards",
        "extractor": "rooter_rsc",
        "variant_pattern": r"Rs\.1000\b",
        "enabled": True,
    }
    base.update(overrides)
    return base


def observation(status=core.IN_STOCK, price=985.0, discount="", cashback="",
                ok=True, error="", note=""):
    return {
        "ok": ok, "status": status, "price": price, "discount": discount,
        "cashback": cashback, "error": error, "note": note,
    }


# =========================================================================== #
# 1. State persistence
# =========================================================================== #
class TestStatePersistence:
    def test_missing_state_file_returns_default(self, tmp_path):
        state = core.load_state(tmp_path / "absent.json")
        assert state["products"] == {}
        assert state["events"] == []
        assert state["chat_id"] == ""
        assert state["last_daily_report_date"] == ""

    def test_round_trip_preserves_contents(self, tmp_path):
        path = tmp_path / "state.json"
        state = core.default_state()
        state["chat_id"] = "123456789"
        state["last_daily_report_date"] = "2026-11-08"
        state["products"]["rooter-psn-1000"] = {
            "status": core.IN_STOCK, "price": 985.0, "discount": "",
            "cashback": "", "last_error": "", "first_seen_at": T0,
            "last_changed_at": T0, "baseline": True, "note": "",
        }
        core.save_state(state, path)

        loaded = core.load_state(path)
        assert loaded["chat_id"] == "123456789"
        assert loaded["last_daily_report_date"] == "2026-11-08"
        entry = loaded["products"]["rooter-psn-1000"]
        assert entry["status"] == core.IN_STOCK
        assert entry["price"] == 985.0
        assert entry["baseline"] is True

    def test_state_is_written_as_valid_json(self, tmp_path):
        path = tmp_path / "state.json"
        core.save_state(core.default_state(), path)
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
        # Sorted keys + trailing newline keeps git diffs stable and readable.
        assert path.read_text(encoding="utf-8").endswith("\n")

    def test_no_temporary_file_left_behind(self, tmp_path):
        path = tmp_path / "state.json"
        core.save_state(core.default_state(), path)
        assert not path.with_suffix(".json.tmp").exists()
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_corrupt_state_file_falls_back_to_default(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{ this is not json", encoding="utf-8")
        state = core.load_state(path)
        assert state["products"] == {}
        assert state["events"] == []

    def test_non_object_state_file_falls_back_to_default(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        state = core.load_state(path)
        assert state["products"] == {}

    def test_unknown_top_level_keys_are_dropped(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({
            "version": 1, "chat_id": "42", "products": {}, "events": [],
            "stale_junk": "should not survive",
        }), encoding="utf-8")
        state = core.load_state(path)
        assert state["chat_id"] == "42"
        assert "stale_junk" not in state

    def test_event_history_is_bounded(self, tmp_path):
        path = tmp_path / "state.json"
        state = core.default_state()
        for i in range(core.MAX_EVENTS + 25):
            core.record_event(state, "p", "RESTOCK", f"msg {i}", T0)
        core.save_state(state, path)
        loaded = core.load_state(path)
        assert len(loaded["events"]) == core.MAX_EVENTS
        # The newest events survive, the oldest are dropped.
        assert loaded["events"][-1]["message"] == f"msg {core.MAX_EVENTS + 24}"

    def test_state_survives_multiple_processes(self, tmp_path):
        """Simulate separate GitHub Actions runs reading/writing the same file."""
        path = tmp_path / "state.json"
        for run in range(5):
            state = core.load_state(path)          # run N starts from disk
            entry = state["products"].setdefault("p", {"baseline": False})
            entry["status"] = core.IN_STOCK
            entry["baseline"] = True
            core.save_state(state, path)           # run N ends by writing disk
        final = core.load_state(path)
        assert final["products"]["p"]["status"] == core.IN_STOCK
        assert final["products"]["p"]["baseline"] is True


# =========================================================================== #
# 2. Stock transitions
# =========================================================================== #
class TestStockTransitions:
    def test_first_observation_establishes_baseline_without_alert(self):
        result = core.evaluate(product(), None, observation(status=core.IN_STOCK), T0)
        assert result.entry["status"] == core.IN_STOCK
        assert result.entry["baseline"] is True
        assert result.alerts == []          # no false alert on the very first run

    def test_first_observation_of_out_of_stock_is_silent(self):
        result = core.evaluate(product(), None, observation(status=core.OUT_OF_STOCK), T0)
        assert result.entry["status"] == core.OUT_OF_STOCK
        assert result.alerts == []

    def test_out_of_stock_then_in_stock_raises_restock(self):
        first = core.evaluate(product(), None, observation(status=core.OUT_OF_STOCK), T0)
        second = core.evaluate(product(), first.entry,
                               observation(status=core.IN_STOCK), T1)
        assert [a.type for a in second.alerts] == ["RESTOCK"]
        assert "BACK IN STOCK" in second.alerts[0].message
        assert second.entry["status"] == core.IN_STOCK

    def test_in_stock_stays_in_stock_without_alert(self):
        first = core.evaluate(product(), None, observation(status=core.IN_STOCK), T0)
        second = core.evaluate(product(), first.entry,
                               observation(status=core.IN_STOCK), T1)
        assert second.alerts == []

    def test_unknown_is_never_reported_as_out_of_stock(self):
        first = core.evaluate(product(), None, observation(status=core.IN_STOCK), T0)
        second = core.evaluate(product(), first.entry,
                               observation(status=core.UNKNOWN, price=None), T1)
        assert second.entry["status"] == core.UNKNOWN
        assert second.entry["status"] != core.OUT_OF_STOCK
        # Going unknown must not raise a restock alert either.
        assert second.alerts == []

    def test_unknown_baseline_then_in_stock_does_not_rest_alert(self):
        """No baseline -> no restock claim; the first known state is a baseline."""
        first = core.evaluate(product(), None, observation(status=core.UNKNOWN), T0)
        assert first.alerts == []
        second = core.evaluate(product(), first.entry,
                               observation(status=core.IN_STOCK), T1)
        assert second.alerts == []

    def test_failed_fetch_never_changes_status(self):
        first = core.evaluate(product(), None, observation(status=core.IN_STOCK, price=985), T0)
        second = core.evaluate(
            product(), first.entry,
            observation(ok=False, status=None, price=None,
                        error="HTTPError: 503 Service Unavailable"),
            T1,
        )
        assert second.entry["status"] == core.IN_STOCK      # unchanged
        assert second.entry["price"] == 985.0               # unchanged
        assert "503" in second.entry["last_error"]
        assert second.alerts == []

    def test_unknown_then_in_stock_is_not_claimed_as_restock(self):
        """UNKNOWN -> IN_STOCK is a discovery, not a witnessed restock."""
        first = core.evaluate(product(), None, observation(status=core.UNKNOWN), T0)
        second = core.evaluate(product(), first.entry,
                               observation(status=core.IN_STOCK), T1)
        assert second.entry["status"] == core.IN_STOCK
        assert second.alerts == []

    def test_out_of_stock_survives_an_unknown_gap_before_restock(self):
        """OUT_OF_STOCK -> UNKNOWN -> IN_STOCK must still alert."""
        state = core.evaluate(product(), None, observation(status=core.OUT_OF_STOCK), T0)
        state = core.evaluate(product(), state.entry,
                              observation(status=core.UNKNOWN), T1)
        assert state.entry["status"] == core.UNKNOWN
        state = core.evaluate(product(), state.entry,
                              observation(status=core.IN_STOCK), T2)
        assert [a.type for a in state.alerts] == ["RESTOCK"]

    def test_failed_fetch_then_in_stock_after_out_of_stock_still_alerts(self):
        state = core.evaluate(product(), None, observation(status=core.OUT_OF_STOCK), T0)
        state = core.evaluate(product(), state.entry,
                              observation(ok=False, error="timeout"), T1)
        assert state.entry["status"] == core.OUT_OF_STOCK
        state = core.evaluate(product(), state.entry,
                              observation(status=core.IN_STOCK), T2)
        assert [a.type for a in state.alerts] == ["RESTOCK"]

    def test_failed_fetch_while_out_of_stock_stays_out_of_stock(self):
        first = core.evaluate(product(), None, observation(status=core.OUT_OF_STOCK), T0)
        second = core.evaluate(product(), first.entry,
                               observation(ok=False, status=None, error="timeout"), T1)
        assert second.entry["status"] == core.OUT_OF_STOCK

    def test_error_then_recovery_does_not_spam(self):
        first = core.evaluate(product(), None, observation(status=core.OUT_OF_STOCK), T0)
        errored = core.evaluate(product(), first.entry,
                                observation(ok=False, error="boom"), T1)
        recovered = core.evaluate(product(), errored.entry,
                                  observation(status=core.OUT_OF_STOCK), T2)
        assert recovered.alerts == []
        assert recovered.entry["last_error"] == ""

    def test_missing_variant_yields_unknown_not_out_of_stock(self):
        first = core.evaluate(product(), None,
                              observation(status=core.UNKNOWN,
                                          note="denomination variant not found on page"), T0)
        assert first.entry["status"] == core.UNKNOWN
        assert first.entry["note"]

    def test_status_change_marks_last_changed_at(self):
        first = core.evaluate(product(), None, observation(status=core.OUT_OF_STOCK), T0)
        assert first.entry["last_changed_at"] == T0
        second = core.evaluate(product(), first.entry,
                               observation(status=core.IN_STOCK), T1)
        assert second.entry["last_changed_at"] == T1
        assert second.changed is True


# =========================================================================== #
# 3. Price drops
# =========================================================================== #
class TestPriceDrops:
    def test_price_drop_raises_alert(self):
        first = core.evaluate(product(), None, observation(price=1000.0), T0)
        second = core.evaluate(product(), first.entry, observation(price=985.0), T1)
        assert [a.type for a in second.alerts] == ["PRICE_DROP"]
        message = second.alerts[0].message
        assert "₹1,000.00" in message
        assert "₹985.00" in message

    def test_price_increase_is_silent(self):
        first = core.evaluate(product(), None, observation(price=985.0), T0)
        second = core.evaluate(product(), first.entry, observation(price=1000.0), T1)
        assert second.alerts == []

    def test_price_unchanged_is_silent(self):
        first = core.evaluate(product(), None, observation(price=985.0), T0)
        second = core.evaluate(product(), first.entry, observation(price=985.0), T1)
        assert second.alerts == []
        assert second.changed is False

    def test_price_appearing_after_unknown_is_silent(self):
        first = core.evaluate(product(), None, observation(status=core.UNKNOWN, price=None), T0)
        second = core.evaluate(product(), first.entry, observation(price=985.0), T1)
        assert [a.type for a in second.alerts] == []   # no old price to compare

    def test_price_is_not_rounded_before_comparison(self):
        first = core.evaluate(product(), None, observation(price=985.50), T0)
        second = core.evaluate(product(), first.entry, observation(price=985.49), T1)
        assert [a.type for a in second.alerts] == ["PRICE_DROP"]

    def test_repeated_runs_at_same_price_alert_once(self):
        state = core.evaluate(product(), None, observation(price=1000.0), T0)
        drops = 0
        for step, price in enumerate([990.0, 990.0, 990.0, 985.0, 985.0], start=1):
            state = core.evaluate(product(), state.entry, observation(price=price),
                                  f"2026-11-08T10:0{step}:00+05:30")
            drops += sum(1 for a in state.alerts if a.type == "PRICE_DROP")
        assert drops == 2      # one for 990, one for 985 - not five


# =========================================================================== #
# 4. Duplicate-alert prevention
# =========================================================================== #
class TestDuplicatePrevention:
    def test_restock_alerts_only_once(self):
        state = core.evaluate(product(), None, observation(status=core.OUT_OF_STOCK), T0)
        repeats = 0
        for i in range(6):
            state = core.evaluate(product(), state.entry,
                                  observation(status=core.IN_STOCK), T1)
            repeats += len(state.alerts)
        assert repeats == 1

    def test_restock_then_out_then_in_alerts_twice(self):
        state = core.evaluate(product(), None, observation(status=core.OUT_OF_STOCK), T0)
        state = core.evaluate(product(), state.entry, observation(status=core.IN_STOCK), T1)
        state = core.evaluate(product(), state.entry, observation(status=core.OUT_OF_STOCK), T2)
        state = core.evaluate(product(), state.entry, observation(status=core.IN_STOCK), T2)
        assert [a.type for a in state.alerts] == ["RESTOCK"]

    def test_discount_change_alerts_once(self):
        state = core.evaluate(product(), None, observation(discount=""), T0)
        state = core.evaluate(product(), state.entry,
                              observation(discount="10% off"), T1)
        assert [a.type for a in state.alerts] == ["DISCOUNT"]
        state = core.evaluate(product(), state.entry,
                              observation(discount="10% off"), T2)
        assert state.alerts == []

    def test_new_discount_text_alerts_again(self):
        state = core.evaluate(product(), None, observation(discount="5% off"), T0)
        state = core.evaluate(product(), state.entry,
                              observation(discount="10% off"), T1)
        assert [a.type for a in state.alerts] == ["DISCOUNT"]

    def test_cashback_change_alerts_once(self):
        state = core.evaluate(product(), None, observation(cashback=""), T0)
        state = core.evaluate(product(), state.entry,
                              observation(cashback="Flat ₹50 back"), T1)
        assert [a.type for a in state.alerts] == ["CASHBACK"]
        state = core.evaluate(product(), state.entry,
                              observation(cashback="Flat ₹50 back"), T2)
        assert state.alerts == []

    def test_combined_scenario_produces_no_duplicates_over_many_runs(self):
        """A realistic sequence: each distinct event fires exactly once."""
        state = core.evaluate(product(), None, observation(status=core.IN_STOCK,
                                                           price=1000.0), T0)
        assert state.alerts == []

        fired: list[str] = []
        # Run 1: price drop. Run 2: restock cycle. Run 3: discount appears.
        # Runs 4-8: identical observations -> silence.
        sequence = [
            observation(status=core.IN_STOCK, price=985.0),
            observation(status=core.OUT_OF_STOCK, price=985.0),
            observation(status=core.IN_STOCK, price=985.0, discount="10% off"),
        ] + [observation(status=core.IN_STOCK, price=985.0, discount="10% off")] * 5

        for i, obs in enumerate(sequence, start=1):
            state = core.evaluate(product(), state.entry, obs, f"2026-11-08T11:{i:02d}:00+05:30")
            fired.extend(a.type for a in state.alerts)

        assert fired == ["PRICE_DROP", "RESTOCK", "DISCOUNT"]

    def test_alert_events_are_recorded_in_state(self):
        state = core.default_state()
        first = core.evaluate(product(), None, observation(status=core.OUT_OF_STOCK), T0)
        state["products"][product()["id"]] = first.entry
        second = core.evaluate(product(), first.entry,
                               observation(status=core.IN_STOCK), T1)
        state["products"][product()["id"]] = second.entry
        for alert in second.alerts:
            core.record_event(state, product()["id"], alert.type, alert.message, T1)
        assert [e["type"] for e in state["events"]] == ["RESTOCK"]


# =========================================================================== #
# 5. Rooter extractor
# =========================================================================== #
def make_rooter_html(variations):
    """Build HTML containing a Next.js flight payload, as Rooter really sends."""
    product_data = {
        "productData": {"name": "PlayStation Gift Cards India",
                        "baseVariations": variations},
    }
    inner = json.dumps(product_data)
    escaped = json.dumps(inner)[1:-1]          # escape for a JSON string literal
    return (
        "<!DOCTYPE html><html><head><title>x</title></head><body>"
        f'<script>self.__next_f.push([1,"{escaped}"])</script>'
        "</body></html>"
    )


def variation(name, out_of_stock, sell, pro=None):
    return {
        "id": "v-" + name,
        "name": name,
        "outOfStock": out_of_stock,
        "sellers": [
            {"priority": 1, "outOfStock": out_of_stock, "costPrice": 1000,
             "sellPrice": sell, "proSellPrice": pro if pro is not None else sell,
             "cashback": []},
        ],
    }


class TestRooterExtractor:
    def test_reads_denomination_and_price(self):
        html = make_rooter_html([variation("Rs.1000 Wallet Code", False, 985)])
        result = core.extract_rooter(html, product())
        assert result["ok"] is True
        assert result["status"] == core.IN_STOCK
        assert result["price"] == 985.0

    def test_out_of_stock_flag_is_honoured(self):
        html = make_rooter_html([variation("Rs.1000 Wallet Code", True, 985)])
        result = core.extract_rooter(html, product())
        assert result["status"] == core.OUT_OF_STOCK

    def test_matching_denomination_is_not_confused_by_prefix(self):
        """'Rs.1000' must not match a 'Rs.10000' variation."""
        html = make_rooter_html([
            variation("Rs.10000 Wallet Code", False, 9999),
            variation("Rs.1000 Wallet Code", False, 985),
        ])
        result = core.extract_rooter(html, product())
        assert result["price"] == 985.0

    def test_missing_denomination_is_unknown_not_out_of_stock(self):
        html = make_rooter_html([variation("Rs.2000 Wallet Code", False, 1970)])
        result = core.extract_rooter(html, product())
        assert result["status"] == core.UNKNOWN
        assert result["price"] is None
        assert "not found" in result["note"]

    def test_no_product_data_is_an_error_not_a_stock_reading(self):
        """A page without product data must raise, never imply a stock state."""
        with pytest.raises(core.ExtractionError):
            core.find_rooter_product_data("<html><body>nothing</body></html>")

        # observe() converts that exception into a failure observation.
        # (fetch is stubbed so no network call is made)
        result = core.observe(
            {"id": "x", "url": "https://example.invalid/p",
             "extractor": "rooter_rsc", "denomination": 1000},
            html="<html><body>nothing</body></html>",
        )
        assert result["ok"] is False
        assert result["status"] is None

    def test_prefers_in_stock_seller_price(self):
        html = make_rooter_html([{
            "name": "Rs.1000 Wallet Code",
            "outOfStock": False,
            "sellers": [
                {"priority": 1, "outOfStock": True, "sellPrice": 985},
                {"priority": 2, "outOfStock": False, "sellPrice": 1000},
            ],
        }])
        result = core.extract_rooter(html, product())
        assert result["price"] == 1000.0     # purchasable seller wins

    def test_rsc_payload_concatenates_multiple_pushes(self):
        html = (
            '<script>self.__next_f.push([1,"hello "])</script>'
            '<script>self.__next_f.push([0])</script>'
            '<script>self.__next_f.push([1,"world"])</script>'
        )
        assert core.rsc_payload(html) == "hello world"

    def test_extract_json_value_handles_braces_in_strings(self):
        text = '{"a": "}", "b": [1, {"c": "}"}]} trailing'
        value, end = core.extract_json_value(text, 0)
        assert json.loads(value)["b"][1]["c"] == "}"
        assert text[end:] == " trailing"


# =========================================================================== #
# 6. Generic HTML extractor
# =========================================================================== #
class TestHtmlExtractor:
    def test_selector_scoped_out_of_stock(self):
        page = ('<div class="buybox"><span id="avail">Out of Stock</span></div>'
                '<p>Other item In Stock</p>')
        result = core.extract_html(page, product(
            extractor="html_text",
            availability_selector="#avail",
            in_stock_patterns=["in stock"],
            out_of_stock_patterns=["out of stock"],
        ))
        assert result["status"] == core.OUT_OF_STOCK

    def test_missing_selector_returns_unknown_not_page_wide_match(self):
        """A selector that matches nothing must not fall back to the page text."""
        page = ('<div class="buybox">Out of Stock</div>'
                '<p>unrelated: out of stock</p>')
        result = core.extract_html(page, product(
            extractor="html_text",
            availability_selector="#does-not-exist",
            in_stock_patterns=["in stock"],
            out_of_stock_patterns=["out of stock"],
        ))
        assert result["status"] == core.UNKNOWN
        assert "matched nothing" in result["note"]

    def test_no_patterns_matched_is_unknown(self):
        result = core.extract_html("<p>some text</p>", product(
            extractor="html_text",
            availability_selector="p",
            in_stock_patterns=["buy now"],
            out_of_stock_patterns=["out of stock"],
        ))
        assert result["status"] == core.UNKNOWN

    def test_price_parsed_from_selector(self):
        page = '<span class="price">₹1,000.00</span><div>In Stock</div>'
        result = core.extract_html(page, product(
            extractor="html_text", availability_selector="div",
            price_selector=".price",
            in_stock_patterns=["in stock"], out_of_stock_patterns=["out"],
        ))
        assert result["price"] == 1000.0


# =========================================================================== #
# 7. observe() never raises
# =========================================================================== #
class TestObserveSafety:
    def test_unknown_extractor_is_a_failure_not_a_stock_reading(self):
        result = core.observe({"id": "x", "url": "http://example.invalid",
                               "extractor": "nope"})
        assert result["ok"] is False
        assert result["status"] is None
        assert "unknown extractor" in result["error"]

    def test_missing_url_is_a_failure(self):
        result = core.observe({"id": "x", "extractor": "html_text"})
        assert result["ok"] is False
        assert "no url" in result["error"]

    def test_network_failure_is_captured_not_raised(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise ConnectionError("refused")

        monkeypatch.setattr(core, "fetch_html", boom)
        result = core.observe({"id": "x", "url": "http://example.invalid",
                               "extractor": "html_text"})
        assert result["ok"] is False
        assert "ConnectionError" in result["error"]

    def test_http_error_status_does_not_become_out_of_stock(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise core.requests.HTTPError("503 Server Error")

        monkeypatch.setattr(core, "fetch_html", boom)
        result = core.observe({"id": "x", "url": "http://example.invalid",
                               "extractor": "html_text"})
        assert result["ok"] is False
        assert result["status"] is None


# =========================================================================== #
# 8. Daily report scheduling
# =========================================================================== #
class TestDailyReportDue:
    @staticmethod
    def _dt(hour, minute, date="2026-11-08"):
        return datetime(2026, 11, 8, hour, minute, tzinfo=TZ) if date == "2026-11-08" \
            else datetime.fromisoformat(f"{date}T{hour:02d}:{minute:02d}:00+05:30")

    def test_not_due_before_report_hour(self):
        from github_runner import report_due
        assert report_due(core.default_state(), self._dt(8, 59), 9) is False

    def test_due_at_report_hour(self):
        from github_runner import report_due
        assert report_due(core.default_state(), self._dt(9, 0), 9) is True

    def test_due_after_report_hour_if_not_sent(self):
        from github_runner import report_due
        assert report_due(core.default_state(), self._dt(14, 30), 9) is True

    def test_not_due_when_already_sent_today(self):
        from github_runner import report_due
        state = core.default_state()
        state["last_daily_report_date"] = "2026-11-08"
        assert report_due(state, self._dt(9, 30), 9) is False
        assert report_due(state, self._dt(23, 59), 9) is False

    def test_due_again_the_next_day(self):
        from github_runner import report_due
        state = core.default_state()
        state["last_daily_report_date"] = "2026-11-07"
        assert report_due(state, self._dt(9, 1, date="2026-11-08"), 9) is True


# =========================================================================== #
# 9. Reporting and secret hygiene
# =========================================================================== #
class TestReportAndSecrets:
    def test_report_shows_unknown_without_claiming_out_of_stock(self):
        state = core.default_state()
        state["products"]["rooter-psn-1000"] = {"status": core.UNKNOWN, "baseline": True}
        text = core.build_report([product()], state, T0, "Daily report")
        assert "UNKNOWN" in text
        assert "OUT_OF_STOCK" not in text

    def test_report_lists_disabled_product_count(self):
        text = core.build_report([product(enabled=False)], core.default_state(), T0, "R")
        assert "No products enabled yet" in text

    def test_report_includes_offer_and_cashback(self):
        state = core.default_state()
        state["products"]["rooter-psn-1000"] = {
            "status": core.IN_STOCK, "price": 985.0,
            "discount": "Pro price ₹993.33", "cashback": "Flat ₹50 back",
            "baseline": True,
        }
        text = core.build_report([product()], state, T0, "R")
        assert "Pro price ₹993.33" in text
        assert "Flat ₹50 back" in text

    def test_scrub_removes_token_from_text(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SECRET-ABC")
        scrubbed = core.scrub("failed url /bot123456:SECRET-ABC/sendMessage")
        assert "SECRET-ABC" not in scrubbed
        assert "***TOKEN***" in scrubbed

    def test_scrub_is_noop_without_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        assert core.scrub("nothing to hide") == "nothing to hide"

    def test_token_comes_from_environment_only(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        assert core.telegram_token() == ""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  abc  ")
        assert core.telegram_token() == "abc"


# =========================================================================== #
# 11. Telegram chat pairing
# =========================================================================== #
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


class _FakeSession:
    """Minimal stand-in for requests.Session used by claim_chat."""

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeResponse(self._payload)


def _update(text, chat_id=111, chat_type="private"):
    return {"message": {"text": text, "chat": {"id": chat_id, "type": chat_type}}}


class TestClaimChat:
    def test_no_updates_returns_empty(self):
        session = _FakeSession({"ok": True, "result": []})
        assert core.claim_chat("tok", session=session) == ""

    def test_plain_start_is_claimed(self):
        session = _FakeSession({"ok": True, "result": [_update("/start")]})
        assert core.claim_chat("tok", session=session) == "111"

    def test_start_with_bot_username_is_claimed(self):
        # Telegram sends "/start@BotName" when picked from the command menu.
        session = _FakeSession(
            {"ok": True, "result": [_update("/start@PSN_Alert_bot")]}
        )
        assert core.claim_chat("tok", session=session) == "111"

    def test_group_chat_is_ignored(self):
        session = _FakeSession(
            {"ok": True, "result": [_update("/start", chat_type="group")]}
        )
        assert core.claim_chat("tok", session=session) == ""

    def test_non_start_message_is_ignored(self):
        session = _FakeSession({"ok": True, "result": [_update("hello")]})
        assert core.claim_chat("tok", session=session) == ""

    def test_start_among_other_updates_still_claims(self):
        session = _FakeSession({
            "ok": True,
            "result": [_update("hello"), _update("/start", chat_id=999)],
        })
        assert core.claim_chat("tok", session=session) == "999"

    def test_conflict_returns_empty_without_raising(self):
        session = _FakeSession({"ok": False, "description": "409: Conflict"})
        assert core.claim_chat("tok", session=session) == ""

    def test_rejected_payload_returns_empty(self):
        session = _FakeSession({"ok": False, "description": "Unauthorized"})
        assert core.claim_chat("tok", session=session) == ""

    def test_empty_token_short_circuits(self):
        session = _FakeSession({"ok": True, "result": [_update("/start")]})
        assert core.claim_chat("", session=session) == ""
        assert session.calls == []

    def test_requests_allowed_updates_filtered_to_message(self):
        session = _FakeSession({"ok": True, "result": []})
        core.claim_chat("tok", session=session)
        params = session.calls[0]["params"]
        assert json.loads(params["allowed_updates"]) == ["message"]


# =========================================================================== #
# 12. Money parsing
# =========================================================================== #
class TestParseMoney:
    @pytest.mark.parametrize("text,expected", [
        ("₹1,000.00", 1000.0),
        ("Rs. 985", 985.0),
        ("INR 5000", 5000.0),
        ("1000 ₹", 1000.0),
        ("₹4,000", 4000.0),
        ("", None),
        (None, None),
        ("no money here", None),
    ])
    def test_values(self, text, expected):
        assert core.parse_money(text) == expected
