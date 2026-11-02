import tempfile
import unittest
from pathlib import Path

from portkit import cdo
from portkit.util import Fail

TSV = """# class\tproperty\towner\ttype\toffset\tvalue
UFGFoo\tmSpeed\tUFGFoo\tfloat\t1000\t5.000000
UFGFoo\tmZero\tUFGFoo\tfloat\t1004\t0.000000
UFGFoo\tmKind\tUFGFoo\tEFoo\t1008\tBar
UFGFoo\tmOld\tUFGFoo\tTEnumAsByte<EOld::Type>\t1009\tEOld::Baz
UFGFoo\tbOn\tUFGFoo\tuint8\t1010\tTrue
UFGFoo\tbOff\tUFGFoo\tuint8\t1010\tFalse
UFGFoo\tmCount\tUFGFoo\tint32\t1012\t3
UFGFoo\tmName\tUFGFoo\tFName\t1016\tNone
UFGFoo\tmTitle\tUFGFoo\tFString\t1024\tSay \\"hi\\"
UFGFoo\tmWidget\tUFGFoo\tTSubclassOf<UFGBar>\t1040\t/Script/FactoryGame.FGBar
UFGFoo\tmIcon\tUFGFoo\tTObjectPtr<UTexture2D>\t1048\t/Game/UI/Icon.Icon
UFGFoo\tmList\tUFGFoo\tTArray<int32>\t1056\t(1,2)
UFGFoo\tmEmpty\tUFGFoo\tTArray<int32>\t1072\t()
UFGFoo\tmOffset\tUFGFoo\tFVector\t1088\t(X=1.500000,Y=0.000000)
UFGFoo\tPrimaryActorTick\tAActor\tFActorTickFunction\t40\t(bCanEverTick=True,TickInterval=0.500000)
UFGFoo\tbReplicates\tAActor\tuint8\t90\tFalse
UFGBar\tmSpeed\tUFGBar\tfloat\t900\t1.000000
"""


class DraftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tsv = Path(self.tmp.name) / "RecompCDO.tsv"
        self.tsv.write_text(TSV, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_constructor_lines(self):
        _, lines = cdo.draft(self.tsv, "UFGFoo")
        self.assertEqual(lines, [
            "mSpeed = 5.0f;",
            "mKind = EFoo::Bar;",
            "mOld = EOld::Baz;",
            "bOn = true;",
            "mCount = 3;",
            'mTitle = TEXT( "Say \\"hi\\"" );',
            "mWidget = UFGBar::StaticClass();",
            "// mIcon = /Game/UI/Icon.Icon   (TObjectPtr<UTexture2D>)",
            "// mList = (1,2)   (TArray<int32>)",
            "mOffset.X = 1.5f;",
            "mOffset.Y = 0.0f;",
            "PrimaryActorTick.bCanEverTick = true;",
            "PrimaryActorTick.TickInterval = 0.5f;",
            "bReplicates = false;",       # inherited: a zero that differs from the parent's is kept
        ])

    def test_an_unknown_class(self):
        with self.assertRaises(Fail):
            cdo.draft(self.tsv, "UFGNope")

    def test_struct_text(self):
        self.assertEqual(cdo.split_fields('(A=1,B=(C=2,D="x,y"),E[1]=3)'), [("A", "1"), ("B", '(C=2,D="x,y")'), ("E[1]", "3")])
        self.assertIsNone(cdo.split_fields("(1,2)"))
        self.assertIsNone(cdo.split_fields("plain"))


if __name__ == "__main__":
    unittest.main()
