import struct
import tempfile
import unittest
from pathlib import Path

from portkit import coff, config, msvc
from portkit.util import Fail

DEBUG_FLAGS = 0x42100040   # initialized data, discardable, align 1, read


def record(kind, body):
    body += b"\xf1" * (-(len(body) + 4) % 4)   # records are padded to 4 bytes
    return struct.pack("<HH", len(body) + 2, kind) + body


def string_id(text, substrings=0):
    return record(msvc.LF_STRING_ID, struct.pack("<I", substrings) + text.encode() + b"\0")


def obj_with_build_info(command, precomp=None):
    """An object whose .debug$T holds a build info record as cl writes it under /Z7: the command line in two
    pieces (an LF_SUBSTR_LIST of one LF_STRING_ID, then the rest), the other four strings empty, as
    /experimental:deterministic leaves them. precomp: (start, count) of an LF_PRECOMP record first."""
    base = 0x1000
    records = []
    if precomp:
        records.append(record(msvc.LF_PRECOMP, struct.pack("<III", *precomp, 0) + b"pch.obj\0"))
        base = precomp[0] + precomp[1]
    first, rest = command[:len(command) // 2], command[len(command) // 2:]
    records += [
        string_id(""),                                                          # base + 0: empty
        string_id(first),                                                       # base + 1
        record(msvc.LF_SUBSTR_LIST, struct.pack("<II", 1, base + 1)),         # base + 2
        string_id(rest, base + 2),                                              # base + 3: the command
        record(msvc.LF_BUILDINFO, struct.pack("<H5I", 5, base, base, base, base, base + 3)),
    ]
    w = coff.Writer()
    w.data(w.section(".debug$T", DEBUG_FLAGS), struct.pack("<I", 4) + b"".join(records))
    text = w.section(".text$mn", coff.CODE_FLAGS)
    w.data(text, b"\xc3")
    w.define("f", text, 0, function=True)
    return w.build()


def obj_with(functions):
    """functions: {name: (code, [offsets of a REL32 relocation])}."""
    w = coff.Writer()
    for name, (code, relocs) in functions.items():
        text = w.section(".text$mn", coff.CODE_FLAGS)
        w.data(text, code)
        w.define(name, text, 0, function=True)
        for off in relocs:
            w.relocate(text, off, coff.REL32, w.external("target"))
    return w.build()


class BuildInfoTest(unittest.TestCase):
    COMMAND = "-external:W0 -Zc:inline -nologo -Oi -Ox -Z7 -bigobj -fp:fast -std:c++20 -X"

    def test_the_command_line_is_its_pieces_joined(self):
        info = msvc.build_info(obj_with_build_info(self.COMMAND))
        self.assertEqual(info.command, self.COMMAND)
        self.assertEqual((info.cwd, info.compiler, info.source, info.pdb), ("", "", "", ""))

    def test_compiled_with_a_pch_the_indexes_follow_the_pch_objects(self):
        info = msvc.build_info(obj_with_build_info(self.COMMAND, precomp=(0x1000, 0xC2A43)))
        self.assertEqual(info.command, self.COMMAND)

    def test_an_object_without_build_info(self):
        with self.assertRaises(Fail):
            msvc.build_info(obj_with({"f": (b"\xc3", [])}))


class ResponseFileTest(unittest.TestCase):
    RSP = ('"D:/P/Source/M/Private/Recovered/Module.FactoryGame.101.cpp"\r\n'
           '@"D:/P/Intermediate/Build/M/M.Shared.rsp"\r\n'
           '/FI"D:/P/Intermediate/Build/Engine/SharedPCH.h"\r\n'
           '/FI"D:/P/Intermediate/Build/M/Definitions.M.h"\r\n'
           '/Yu"D:/P/Intermediate/Build/Engine/SharedPCH.h"\r\n'
           '/Fp"D:/P/Intermediate/Build/Engine/SharedPCH.h.pch"\r\n'
           '/Fo"D:/P/Intermediate/Build/M/Module.FactoryGame.101.cpp.obj"\r\n'
           '/experimental:log "D:/P/Intermediate/Build/M/Module.FactoryGame.101.cpp.sarif"\r\n'
           '/sourceDependencies "D:/P/Intermediate/Build/M/Module.FactoryGame.101.cpp.dep.json"\r\n'
           '/Zc:inline\r\n/Ox\r\n/D_WINDLL\r\n/errorReport:prompt\r\n/Z7\r\n/bigobj\r\n')
    SHARED = '/I "."\r\n/I "../Plugins/X/Intermediate/Build/Win64/G/Inc/X"\r\n/external:I "C:/Program Files (x86)/Windows Kits/10/include/10.0.22621.0/ucrt"\r\n'

    def test_windows_quoting(self):
        self.assertEqual(msvc.split_args('/I "a b" /FI"c d" e\\f "g\\"h"'), ["/I", "a b", "/FIc d", "e\\f", 'g"h'])

    def test_nested_response_files_and_separate_values(self):
        cmd = msvc.Command.parse(msvc.split_args(self.RSP), read=lambda p: self.SHARED)
        self.assertEqual(cmd.values("/I"), [".", "../Plugins/X/Intermediate/Build/Win64/G/Inc/X"])
        self.assertEqual(len(cmd.values("/external:I")), 1)
        self.assertEqual(len(cmd.values("/FI")), 2)
        self.assertEqual(cmd.sources, ["D:/P/Source/M/Private/Recovered/Module.FactoryGame.101.cpp"])

    def test_recorded_flags_missing_from_the_response_file(self):
        cmd = msvc.Command.parse(msvc.split_args(self.RSP), read=lambda p: self.SHARED)
        self.assertEqual(msvc.missing_flags("-Zc:inline -Ox -errorreport:prompt -Z7 -bigobj -Ob2", cmd), ["-Ob2"])

    def test_x_is_what_an_empty_include_variable_records(self):
        cmd = msvc.Command.parse(msvc.split_args(self.RSP), read=lambda p: self.SHARED)
        self.assertEqual(msvc.missing_flags("-Ox -X", cmd), [])

    def test_the_mac_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            m = msvc.PathMap(tmp / "project", mirror=tmp / "mirror", engine=tmp / "engine", msvc=tmp / "msvc")
            defs = m.mac("D:/P/Intermediate/Build/M/Definitions.M.h")
            defs.parent.mkdir(parents=True)
            defs.write_text("#define WITH_CHEATS 1\n#define RECOMP_HOOKS 1\n")
            shared = m.mac("D:/P/Intermediate/Build/M/M.Shared.rsp")
            shared.write_text(self.SHARED)
            saved = config.PC_PROJECT
            config.PC_PROJECT = "D:/P"
            try:
                m = msvc.PathMap(tmp / "project", mirror=tmp / "mirror", engine=tmp / "engine", msvc=tmp / "msvc")
                cmd = msvc.command_for(tmp / "project", "M", "Module.FactoryGame.101.cpp", tmp / "out", mapper=m,
                                       text=self.RSP)
            finally:
                config.PC_PROJECT = saved
            lines = cmd.lines()
            self.assertFalse([x for x in lines if x.startswith(("/Yu", "/Fp", "/experimental:log"))])
            self.assertIn(f'/Fo"{msvc.wine_path(tmp / "out" / "Module.FactoryGame.101.cpp.obj")}"', lines)
            self.assertIn(f'/FI"{msvc.wine_path(tmp / "out" / "Definitions.M.h")}"', lines)
            self.assertIn("RECOMP_HOOKS 0", (tmp / "out" / "Definitions.M.h").read_text())
            self.assertEqual(lines[0], msvc.wine_path(tmp / "project" / "Source/M/Private/Recovered/Module.FactoryGame.101.cpp"))
            self.assertIn(f'/I {msvc.quote(msvc.wine_path(tmp / "engine" / "Engine" / "Source"))}', lines)


class PathMapTest(unittest.TestCase):
    def setUp(self):
        self.m = msvc.PathMap("/stage", mirror="/mirror", engine="/engine", msvc="/msvc")

    def test_relative_to_the_engines_source_folder(self):
        self.assertEqual(self.m.pc("Runtime/Core/Public"), f"{config.PC_ENGINE}/Engine/Source/Runtime/Core/Public")
        self.assertEqual(self.m.mac("Runtime/Core/Public"), Path("/engine/Engine/Source/Runtime/Core/Public"))

    def test_generated_folders_come_from_the_mirror(self):
        p = "../Plugins/FX/Niagara/Intermediate/Build/Win64/FactoryGameSteam/Inc/Niagara/UHT"
        self.assertEqual(self.m.mac(p), Path("/mirror/pc/D") / config.PC_ENGINE[3:] / "Engine" / p[3:])
        self.assertEqual(self.m.mac(f"{config.PC_PROJECT}/Intermediate/Build/x.h"),
                         Path("/mirror/pc/D") / config.PC_PROJECT[3:] / "Intermediate/Build/x.h")

    def test_project_toolchain_and_sdk(self):
        self.assertEqual(self.m.mac(f"{config.PC_PROJECT}/Source/FactoryGame/Public/FGMenuBase.h"),
                         Path("/stage/Source/FactoryGame/Public/FGMenuBase.h"))
        self.assertEqual(self.m.mac(config.PC_MSVC.replace("/", "\\") + "\\include"),
                         Path(f"/msvc/MSVC/{config.MSVC_VERSION}/include"))
        self.assertEqual(self.m.mac("C:/Program Files (x86)/Windows Kits/10/include/10.0.22621.0/um"),
                         Path("/msvc/sdk/include/10.0.22621.0/um"))

    def test_an_unknown_root(self):
        with self.assertRaises(Fail):
            self.m.mac("E:/elsewhere/x.h")


class BlobTest(unittest.TestCase):
    def test_numbers_names_and_recovered_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "Source/FactoryGameRecomp/Private/Recovered"
            rec.mkdir(parents=True)
            (rec / "Module.FactoryGame.107.cpp").write_text('#include "FGGameMode.cpp"\n#include "FGMenuBase.cpp"\n')
            self.assertEqual(msvc.blob_name("101", tmp), "Module.FactoryGame.101.cpp")
            self.assertEqual(msvc.blob_name("Module.FactoryGame.99.cpp", tmp), "Module.FactoryGame.99.cpp")
            self.assertEqual(msvc.blob_name("FGMenuBase.cpp", tmp), "Module.FactoryGame.107.cpp")
            with self.assertRaises(Fail):
                msvc.blob_name("FGNotThere.cpp", tmp)

    def test_definitions_for_either_build(self):
        text = "#define A 1\n#define RECOMP_HOOKS 1\n"
        self.assertIn("#define RECOMP_HOOKS 0", msvc.patch_definitions(text))
        self.assertIn("#define RECOMP_HOOKS 1", msvc.patch_definitions(text.replace(" 1\n", " 0\n"), hooks=True))


class CompareTest(unittest.TestCase):
    def test_same_code_but_relocations(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.obj", Path(tmp) / "b.obj"
            a.write_bytes(obj_with({"f": (b"\xe8\x01\x02\x03\x04\xc3", [1]), "g": (b"\x31\xc0\xc3", []),
                                    "h": (b"\xc3", [])}))
            b.write_bytes(obj_with({"f": (b"\xe8\x00\x00\x00\x00\xc3", [1]), "g": (b"\x33\xc0\xc3", []),
                                    "pch": (b"\xc3", [])}))
            self.assertEqual(dict(msvc.compare_objects(a, b)),
                             {"f": "same", "g": "differs", "h": "only-a", "pch": "only-b"})


if __name__ == "__main__":
    unittest.main()
