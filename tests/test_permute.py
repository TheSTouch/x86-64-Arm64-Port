import random
import tempfile
import unittest
from pathlib import Path

from portkit import config, permute
from portkit.util import Fail

SOURCE = """#include "Foo.h"

UBar* UFoo::GetBar( const UObject* context )
{
	if( UWorld* world = GEngine->GetWorld( context, 1 ) ) { return Cast< UBar >( world->GetInstance() ); }
	return nullptr;
}

float UFoo::GetLimit()
{
	// The engine's own setting is only the default: { is not a brace here
	return GetValue( NAME, FVariant( (int32)Limit ) ).GetValue< int32 >();
}

void UFoo::Tick( float dt )
{
	mA = 1;
	mB = dt;
	const int32 n = Count();
	Use( n );
	if( mReady )
	{
		Go();
	}
	else
	{
		Wait();
	}
}
"""


def body_of(name):
    a, b = permute.find_function(SOURCE, name)
    return SOURCE[a:b]


class FindTest(unittest.TestCase):
    def test_bodies_by_qualified_name(self):
        self.assertTrue(body_of("UFoo::GetBar").startswith("{\n\tif( UWorld*"))
        self.assertTrue(body_of("UFoo::GetBar").endswith("return nullptr;\n}"))
        self.assertIn("GetValue< int32 >", body_of("UFoo::GetLimit"))
        with self.assertRaises(Fail):
            permute.find_function(SOURCE, "UFoo::Missing")

    def test_statements(self):
        body = body_of("UFoo::Tick")
        sts = [body[a:b] for a, b in permute.statements(body)]
        self.assertEqual(sts[:4], ["mA = 1;", "mB = dt;", "const int32 n = Count();", "Use( n );"])
        self.assertTrue(sts[4].startswith("if( mReady )") and sts[4].endswith("Wait();\n\t}"))


class RewriteTest(unittest.TestCase):
    rng = random.Random(1)

    def test_split_an_if_with_an_initializer_and_back(self):
        split = permute.r_split_if_init(body_of("UFoo::GetBar"), self.rng)
        self.assertIn("UWorld* world = GEngine->GetWorld( context, 1 );\n\tif( world )", split)
        self.assertEqual(permute.r_split_if_init(split, self.rng), body_of("UFoo::GetBar"))

    def test_invert_an_early_return(self):
        split = permute.r_split_if_init(body_of("UFoo::GetBar"), self.rng)
        inverted = permute.r_invert_early_return(split, self.rng)
        self.assertIn("if( !( world ) ) { return nullptr; }\n\treturn Cast< UBar >( world->GetInstance() );", inverted)

    def test_introduce_and_remove_a_temporary(self):
        body = body_of("UFoo::GetLimit")
        with_temp = permute.r_introduce_temp(body, random.Random(3))
        self.assertRegex(with_temp, r"(const auto|auto&&|auto) tmp\d+ = ")
        self.assertIn("return tmp", with_temp)
        back = permute.r_remove_temp(with_temp, self.rng)
        self.assertNotIn("tmp", back)
        self.assertNotIn("( GetValue", back)   # a call put back needs no parentheses

    def test_swap_independent_statements_only(self):
        body = body_of("UFoo::Tick")
        for seed in range(20):
            swapped = permute.r_swap_statements(body, random.Random(seed))
            self.assertLess(swapped.index("const int32 n"), swapped.index("Use( n )"))
            self.assertNotEqual(swapped, body)

    def test_swap_if_and_else(self):
        swapped = permute.r_swap_if_else(body_of("UFoo::Tick"), self.rng)
        self.assertIn("if( !( mReady ) )\n\t{\n\t\tWait();\n\t}\n\telse\n\t{\n\t\tGo();\n\t}", swapped)

    def test_casts_and_const(self):
        cast = permute.r_cast(body_of("UFoo::GetLimit"), random.Random(0))
        self.assertIn("static_cast< int32 >( Limit )", cast)
        self.assertIn("\tint32 n = Count();", permute.r_const(body_of("UFoo::Tick"), self.rng))

    def test_mutate_changes_something(self):
        rng = random.Random(5)
        for name in ("UFoo::GetBar", "UFoo::GetLimit", "UFoo::Tick"):
            self.assertNotEqual(permute.mutate(body_of(name), rng), body_of(name))


class GuardTest(unittest.TestCase):
    def test_never_in_the_recovered_code_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = config.RECOVERED_REPO
            config.RECOVERED_REPO = Path(tmp)
            try:
                with self.assertRaises(Fail):
                    permute.permute(Path(tmp) / "Project", "FGFoo.cpp", "UFGFoo::Bar")
            finally:
                config.RECOVERED_REPO = saved


if __name__ == "__main__":
    unittest.main()
