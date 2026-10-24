import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "gx_works2_mcp_server.py"
SPEC = importlib.util.spec_from_file_location("gx_works2_mcp_server_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


SAMPLE = [
    {"statement": "Start/stop seal-in", "instructions": ["LD X0", "OR M0", "ANI X1", "OUT M0"]},
    {"statement": "Drive output", "instructions": ["LD M0", "OUT Y0"]},
]


def csv_for(rungs):
    report = MODULE.validate_and_analyze_program(rungs)
    if not report.valid:
        raise AssertionError(report.errors)
    return MODULE.generate_gxworks2_ladder_csv(report.normalized_rungs)


class MockController:
    def __init__(self, original_csv, build_failure_once=False, readback_mismatch=False,
                 import_failure=False, rollback_failure=False, backup_failure=False,
                 check_failure=False):
        self.current_csv = original_csv
        self.build_failure_once = build_failure_once
        self.readback_mismatch = readback_mismatch
        self.import_failure = import_failure
        self.rollback_failure = rollback_failure
        self.backup_failure = backup_failure
        self.check_failure = check_failure
        self.import_count = 0
        self.build_count = 0
        self.check_count = 0

    def connect(self):
        return True, "MELSOFT Series GX Works2 C:\\temp\\test.gxw - [[PRG]Write MAIN 1 Step]"

    def project_key_from_title(self, title):
        return MODULE.GXWorks2Controller.project_key_from_title(title)

    def main_editor_is_active(self, expected_project=None):
        return True, "MELSOFT Series GX Works2 C:\\temp\\test.gxw - [[PRG]Write MAIN 1 Step]"

    def export_current_csv(self, path):
        if self.backup_failure and path.name.startswith("main-before"):
            return False, "simulated backup failure"
        content = self.current_csv
        if self.readback_mismatch and path.name.startswith("verify-MAIN"):
            content = csv_for([{"instructions": ["LD X0", "OUT Y0"]}])
        MODULE.save_utf16le_csv(content, path)
        return True, str(path)

    def import_csv_into_active_editor(self, path):
        if self.import_failure and self.import_count == 0:
            self.import_count += 1
            self.import_started = True
            return False, "simulated import failure"
        if self.rollback_failure and self.import_count > 0:
            self.import_count += 1
            self.import_started = True
            return False, "simulated rollback import failure"
        raw = path.read_bytes()
        self.current_csv = raw[2:].decode("utf-16-le")
        self.import_count += 1
        self.import_started = True
        return True, "candidate selected"

    def compile_build(self):
        self.build_count += 1
        if self.build_failure_once and self.build_count == 1:
            return False, ["Build failed"]
        return True, ["Build completed"]

    def run_check_program(self):
        self.check_count += 1
        if self.check_failure and self.check_count == 1:
            return {"passed": False, "dialog_messages": [], "findings": ["simulated check failure"]}
        return {"passed": True, "dialog_messages": ["No error found"], "findings": []}


class ValidatorTests(unittest.TestCase):
    def test_supported_plc_families_use_family_specific_addressing(self):
        for plc_type in ("QCPU", "LCPU", "Q03UDCPU", "L26CPU-BT"):
            with self.subTest(plc_type=plc_type):
                report = MODULE.validate_and_analyze_program(
                    [{"instructions": ["LD X8", "MOV K1 D0"]}],
                    plc_type=plc_type,
                )
                self.assertTrue(report.valid, report.errors)
        self.assertFalse(
            MODULE.validate_and_analyze_program(
                [{"instructions": ["LD X8", "OUT Y0"]}], plc_type="FX3U/FX3UC"
            ).valid
        )

    def test_unknown_plc_type_is_rejected(self):
        report = MODULE.validate_and_analyze_program(
            [{"instructions": ["LD X0", "OUT Y0"]}], plc_type="UNKNOWN-PLC"
        )
        self.assertFalse(report.valid)
        self.assertIn("Unsupported plc_type", report.errors[0])

    def test_representative_ladder_passes(self):
        report = MODULE.validate_and_analyze_program(SAMPLE)
        self.assertTrue(report.valid, report.errors)
        self.assertEqual(len(report.normalized_rungs), 2)

    def test_rejects_invalid_shapes_opcodes_devices_and_operands(self):
        cases = [
            (None, None),
            ([{"instructions": ["LD X0", 3, "OUT Y0"]}], None),
            ([{"instructions": "LD X0\nOUT Y0"}], None),
            ([{"instructions": ["LD X8", "OUT Y0"]}], None),
            ([{"instructions": ["LD X400", "OUT Y0"]}], None),
            ([{"instructions": ["LD K1", "OUT Y0"]}], None),
            ([{"instructions": ["LD X0", "OUT D0"]}], None),
            ([{"instructions": ["LD X0", "TON T0 K10"]}], None),
            ([{"instructions": ["LD X0", "OUT T0 M0"]}], None),
            ([{"instructions": ["LD X0", "OUT T0 K-1"]}], None),
            ([{"instructions": ["LD X0", "MPS", "OUT Y0"]}], None),
            ([{"instructions": ["LD X0", "OUT Y0", "AND X1"]}], None),
            ([{"statement": "bad\tstatement", "instructions": ["LD X0", "OUT Y0"]}], None),
            ([{"instructions": ["LD X0", "OUT Y0"]}], {"X0": "bad\ncomment"}),
            ([{"instructions": ["LD X0", "OUT Y0"]}], {"X0": "x" * 33}),
            ([{"instructions": ["LD X0", "OUT Y0"]}], {"X0": "a", "x000": "b"}),
        ]
        for rungs, comments in cases:
            with self.subTest(rungs=rungs, comments=comments):
                self.assertFalse(MODULE.validate_and_analyze_program(rungs, comments).valid)

    def test_target_range_boundaries(self):
        self.assertTrue(MODULE.validate_and_analyze_program([{"instructions": ["LD X377", "OUT Y377"]}]).valid)
        self.assertFalse(MODULE.validate_and_analyze_program([{"instructions": ["LD X400", "OUT Y0"]}]).valid)
        self.assertFalse(MODULE.validate_and_analyze_program([{"instructions": ["LD M7680", "OUT Y0"]}]).valid)
        self.assertFalse(MODULE.validate_and_analyze_program([{"instructions": ["LD D8512", "OUT Y0"]}]).valid)


class CsvTests(unittest.TestCase):
    def test_global_device_comments_round_trip_with_plc_metadata(self):
        comments = {"X8": "START", "D10": "SETPOINT", "W1A": "STATUS WORD"}
        with tempfile.TemporaryDirectory() as temp:
            path = MODULE.save_utf16le_csv(
                MODULE.generate_gxworks2_comment_csv(comments, plc_type="QCPU"),
                Path(temp) / "global-comments.csv",
            )
            parsed = MODULE.parse_gxworks2_comment_csv(path)
            self.assertNotIn("error", parsed)
            self.assertEqual(parsed["plc_information"], "QCPU QCPU")
            self.assertEqual(parsed["comments"], comments)

    def test_global_device_comments_reject_wrong_family_addressing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad-comments.csv"
            content = MODULE.generate_gxworks2_comment_csv({"X8": "NOT OCTAL"}, plc_type="QCPU")
            path.write_bytes(b"\xff\xfe" + content.encode("utf-16-le"))
            parsed = MODULE.parse_gxworks2_comment_csv(path)
            self.assertNotIn("error", parsed)
            self.assertEqual(parsed["comments"]["X8"], "NOT OCTAL")

    def test_global_device_comments_reject_duplicate_addresses(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "duplicate-comments.csv"
            content = MODULE.generate_gxworks2_comment_csv({"X1": "A"}, plc_type="QCPU")
            content = content.replace('"X1"\t"A"', '"X1"\t"A"\r\n"X001"\t"B"')
            path.write_bytes(b"\xff\xfe" + content.encode("utf-16-le"))
            parsed = MODULE.parse_gxworks2_comment_csv(path)
            self.assertIn("error", parsed)
            self.assertIn("Duplicate", parsed["error"])

    def test_csv_header_uses_selected_plc_family(self):
        q_csv = MODULE.generate_gxworks2_ladder_csv(
            MODULE.validate_and_analyze_program(
                SAMPLE, plc_type="QCPU"
            ).normalized_rungs,
            plc_type="QCPU",
        )
        self.assertIn('"PLC Information:"\t"QCPU QCPU"', q_csv)

    def test_candidate_roundtrip_preserves_instructions_and_statements(self):
        with tempfile.TemporaryDirectory() as temp:
            path = MODULE.save_utf16le_csv(csv_for(SAMPLE), Path(temp) / "ladder.csv")
            parsed = MODULE.parse_gxworks2_ladder_csv(path)
            self.assertNotIn("error", parsed)
            expected = MODULE.candidate_signatures(MODULE.validate_and_analyze_program(SAMPLE).normalized_rungs)
            self.assertEqual(MODULE.instruction_signature(parsed["items"]), expected[0])
            self.assertEqual(MODULE.statement_signature(parsed["statements"]), expected[1])

    def test_rejects_bad_encoding_header_rows_steps_and_missing_end(self):
        invalid_blobs = [
            b"not bom",
            b"\xff\xfe" + "bad".encode("utf-16-le"),
            b"\xff\xfe" + '"Step No."\t"Wrong"\r\n'.encode("utf-16-le"),
            b"\xff\xfe" + '"Step No."\t"Line Statement"\t"Instruction"\t"I/O(Device)"\t"Blank"\t"PI Statement"\t"Note"\r\n"x"\t""\t"LD"\t"X0"\t""\t""\t""\r\n'.encode("utf-16-le"),
            b"\xff\xfe" + '"Step No."\t"Line Statement"\t"Instruction"\t"I/O(Device)"\t"Blank"\t"PI Statement"\t"Note"\r\n"0"\t""\t"LD"\t"X0"\t""\t""\t""\r\n'.encode("utf-16-le"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            for index, blob in enumerate(invalid_blobs):
                path = Path(temp) / f"bad{index}.csv"
                path.write_bytes(blob)
                with self.subTest(index=index):
                    self.assertIn("error", MODULE.parse_gxworks2_ladder_csv(path))

    def test_never_overwrites_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixed.csv"
            MODULE.save_utf16le_csv("first", path)
            with self.assertRaises(FileExistsError):
                MODULE.save_utf16le_csv("second", path)
            self.assertEqual(path.read_bytes()[2:].decode("utf-16-le"), "first")


class TransactionTests(unittest.TestCase):
    def run_write(self, mock_controller, rungs=SAMPLE):
        old_controller = MODULE.controller
        old_workspace = MODULE.WORKSPACE_DIR
        with tempfile.TemporaryDirectory() as temp:
            MODULE.controller = mock_controller
            MODULE.WORKSPACE_DIR = Path(temp)
            MODULE.GUI_ACTIVE_WRITE.clear()
            try:
                return json.loads(MODULE.gx_write_program(rungs=rungs))
            finally:
                MODULE.controller = old_controller
                MODULE.WORKSPACE_DIR = old_workspace
                MODULE.GUI_ACTIVE_WRITE.clear()

    def test_verified_transaction_returns_success(self):
        result = self.run_write(MockController(csv_for([{"instructions": ["LD X0", "OUT Y0"]}])))
        self.assertEqual(result["status"], "SUCCESS", result)

    def test_build_failure_rolls_back_and_verifies_backup(self):
        original = csv_for([{"instructions": ["LD X0", "OUT Y0"]}])
        mock = MockController(original, build_failure_once=True)
        result = self.run_write(mock)
        self.assertEqual(result["status"], "BUILD_FAILED", result)
        self.assertTrue(result["rollback"]["restored"], result)
        self.assertEqual(mock.import_count, 2)

    def test_readback_mismatch_never_returns_success(self):
        original = csv_for([{"instructions": ["LD X0", "OUT Y0"]}])
        result = self.run_write(MockController(original, readback_mismatch=True))
        self.assertEqual(result["status"], "READBACK_MISMATCH", result)
        self.assertTrue(result["rollback"]["restored"], result)

    def test_import_failure_attempts_verified_rollback(self):
        original = csv_for([{"instructions": ["LD X0", "OUT Y0"]}])
        result = self.run_write(MockController(original, import_failure=True))
        self.assertEqual(result["status"], "WRITE_FAILED", result)
        self.assertTrue(result["rollback"]["restored"], result)

    def test_failed_rollback_is_reported_as_failure(self):
        original = csv_for([{"instructions": ["LD X0", "OUT Y0"]}])
        result = self.run_write(MockController(original, build_failure_once=True, rollback_failure=True))
        self.assertEqual(result["status"], "BUILD_FAILED", result)
        self.assertFalse(result["rollback"]["restored"], result)

    def test_backup_failure_prevents_import(self):
        mock = MockController(csv_for([{"instructions": ["LD X0", "OUT Y0"]}]), backup_failure=True)
        result = self.run_write(mock)
        self.assertEqual(result["status"], "BACKUP_FAILED", result)
        self.assertEqual(mock.import_count, 0)

    def test_program_check_failure_attempts_rollback(self):
        original = csv_for([{"instructions": ["LD X0", "OUT Y0"]}])
        result = self.run_write(MockController(original, check_failure=True))
        self.assertEqual(result["status"], "PROGRAM_CHECK_FAILED", result)
        self.assertTrue(result["rollback"]["restored"], result)

    def test_invalid_input_never_connects_to_gui(self):
        class NoConnect:
            def connect(self):
                raise AssertionError("GUI was touched for rejected input")
        old = MODULE.controller
        MODULE.controller = NoConnect()
        try:
            result = json.loads(MODULE.gx_write_program(rungs=[{"instructions": ["LD X8", "OUT Y0"]}]))
            self.assertEqual(result["status"], "REJECTED_BEFORE_WRITE")
            for options in ({"plc_type": "FX3G"}, {"delivery_mode": None}, {"run_compile_and_check": 0}):
                result = json.loads(MODULE.gx_write_program(rungs=SAMPLE, **options))
                self.assertEqual(result["status"], "REJECTED_BEFORE_WRITE")
        finally:
            MODULE.controller = old

    def test_gui_calls_are_serialized(self):
        calls = []
        entered = threading.Event()

        @MODULE.serialized_gui
        def operation(name):
            calls.append((name, "start"))
            if name == "first":
                entered.set()
                time.sleep(0.15)
            calls.append((name, "end"))

        first = threading.Thread(target=operation, args=("first",))
        second = threading.Thread(target=operation, args=("second",))
        first.start()
        entered.wait(1)
        second.start()
        first.join(2)
        second.join(2)
        self.assertEqual(calls, [("first", "start"), ("first", "end"), ("second", "start"), ("second", "end")])


class GuiResultTests(unittest.TestCase):
    def test_build_without_explicit_success_is_failure(self):
        c = MODULE.GXWorks2Controller()
        c.connect = lambda: (True, "title")
        c.main_editor_is_active = lambda expected_project=None: (True, "title")
        c.main_win = type("Main", (), {"set_focus": lambda self: None, "menu_select": lambda self, item: None})()
        c._dismiss_popups = lambda confirm=True, timeout=0.8: []
        old_send_keys = MODULE.send_keys if hasattr(MODULE, "send_keys") else None
        MODULE.send_keys = lambda *args, **kwargs: None
        try:
            ok, messages = c.compile_build()
        finally:
            if old_send_keys is not None:
                MODULE.send_keys = old_send_keys
        self.assertFalse(ok)
        self.assertIn("explicit GX Works2 build-success", messages[-1])
        c._dismiss_popups = lambda confirm=True, timeout=0.8: ["Build completed"]
        self.assertTrue(c.compile_build()[0])

    def test_unknown_modal_is_not_dismissed_as_confirmation(self):
        class Child:
            def __init__(self, text, kind): self.text, self.kind = text, kind
            def window_text(self): return self.text
            def friendly_class_name(self): return self.kind

        class Dialog:
            handle = 2
            def window_text(self): return "Unexpected"
            def class_name(self): return "#32770"
            def is_enabled(self): return True
            def children(self): return [Child("Continue", "Button")]

        class App:
            def windows(self, visible_only=True): return [Dialog()]

        c = MODULE.GXWorks2Controller()
        c.app = App()
        c.main_win = type("Main", (), {"handle": 1})()
        with self.assertRaisesRegex(RuntimeError, "Unrecognized modal dialog"):
            c._dismiss_popups()

    def test_program_check_requires_explicit_zero_error_evidence(self):
        class Button:
            def friendly_class_name(self): return "Button"
            def window_text(self): return "Execute"
            def click(self): pass

        class CheckDialog:
            def exists(self, timeout=0): return True
            def set_focus(self): pass
            def descendants(self): return [Button()]

        class App:
            def window(self, title_re=None): return CheckDialog()

        c = MODULE.GXWorks2Controller()
        c.connect = lambda: (True, "title")
        c.main_editor_is_active = lambda expected_project=None: (True, "title")
        c.main_win = type("Main", (), {"set_focus": lambda self: None})()
        c.app = App()
        old_send_keys = MODULE.send_keys
        MODULE.send_keys = lambda *args, **kwargs: None
        try:
            c._dismiss_popups = lambda confirm=True, timeout=0.8: []
            self.assertFalse(c.run_check_program()["passed"])
            c._dismiss_popups = lambda confirm=True, timeout=0.8: ["No error found"]
            self.assertTrue(c.run_check_program()["passed"])
            c._dismiss_popups = lambda confirm=True, timeout=0.8: ["Error: 1"]
            self.assertFalse(c.run_check_program()["passed"])
        finally:
            MODULE.send_keys = old_send_keys


if __name__ == "__main__":
    unittest.main()
