"""The permuter's targeted rules (permute.targeted): deterministic rewrites, one candidate per site, each tried
before any random one. Made-up functions only."""

import unittest

from portkit import permute

CTOR = """AWidgetHolder::AWidgetHolder( const FObjectInitializer& ObjectInitializer )
	: Super( ObjectInitializer )
	, mOffset( ForceInitToZero )
	, mCount( 3 )
{
	mSpeed = 2.f;
	ComponentTags.Add( FName( "IgnoreMe" ) );
	Label = FString( TEXT( "Holder" ) );
	bReady = false;
}"""

HEADER = """#pragma once
UCLASS()
class GAME_API AWidgetHolder : public AActor
{
	GENERATED_BODY()
public:
	AWidgetHolder( const FObjectInitializer& ObjectInitializer );
	virtual void Tick( float dt ) override;
	static int32 sInstances;
	UPROPERTY()
	FVector mOffset;
	TObjectPtr< UThing > mSlots[ 4 ];
	int32 mCount;
	float mSpeed = 1.f;
	UPROPERTY( EditDefaultsOnly )
	TObjectPtr< UThing > mOwner;
	uint8 bReady : 1;
	FString Label;
	friend class FHolderHelper;
	void Helper() const { return; }
};
"""

LOOP = """void UFoo::Clean()
{
	int32 removed = 0;
	for( int32 i = 0; i < mItems.Num(); ++i )
	{
		removed += mItems[ i ];
	}
	for( IThing* thing : mThings )
	{
		thing->Release();
	}
	for( const FBar& bar : mBars )
	{
		Use( bar );
	}
	bool bAny = removed > 0;
	Log( removed );
	if( bAny || mForce ) { Notify( mOwner, removed, 2 ); }
	if( GetWorld()->IsGame() ) { Go(); }
	const FString msg = TEXT( "Cleaned " ) + Name;
	Send( msg );
}"""


def texts(rule, definition, **kw):
    return [t for t in rule(definition, **kw)]


class DefinitionTest(unittest.TestCase):
    def test_parts_of_a_constructor(self):
        d = permute.Definition(CTOR)
        self.assertEqual(d.head, "AWidgetHolder::AWidgetHolder( const FObjectInitializer& ObjectInitializer )")
        self.assertEqual([e.name for e in d.init], ["Super", "mOffset", "mCount"])
        self.assertTrue(d.body.startswith("{\n\tmSpeed = 2.f;") and d.body.endswith("}"))
        self.assertEqual(d.cls, "AWidgetHolder")
        self.assertTrue(d.constructor)

    def test_a_brace_initializer_in_the_init_list_is_not_the_body(self):
        d = permute.Definition("FFoo::FFoo()\n\t: mPair{ 0, 0 }\n{\n\tGo();\n}")
        self.assertEqual([e.name for e in d.init], ["mPair"])
        self.assertEqual(d.body, "{\n\tGo();\n}")

    def test_find_definition_spans_the_signature(self):
        text = "#include \"A.h\"\n\n" + LOOP + "\n\nvoid UFoo::Other()\n{\n}\n"
        start, end = permute.find_definition(text, "UFoo::Clean")
        self.assertEqual(text[start:end], LOOP)


class MembersTest(unittest.TestCase):
    def test_members_in_declaration_order(self):
        members = permute.class_members(HEADER, "AWidgetHolder")
        self.assertEqual([(m.name, m.array, m.initialized) for m in members],
                         [("mOffset", False, False), ("mSlots", True, False), ("mCount", False, False),
                          ("mSpeed", False, True), ("mOwner", False, False), ("bReady", False, False),
                          ("Label", False, False)])
        self.assertEqual(permute.class_members(HEADER, "AOther"), None)


class InitListTest(unittest.TestCase):
    def test_a_body_assignment_moves_into_the_init_list_in_member_order(self):
        members = permute.class_members(HEADER, "AWidgetHolder")
        out = texts(permute.t_init_in, CTOR, members=members)
        moved = [t for t in out if "mSpeed( 2.f )" in t][0]
        self.assertIn("\t, mCount( 3 )\n\t, mSpeed( 2.f )\n{", moved)
        self.assertNotIn("mSpeed = 2.f;", moved)
        self.assertFalse(any("ComponentTags(" in t for t in out))   # a call is not an assignment

    def test_an_init_list_entry_moves_into_the_body(self):
        out = texts(permute.t_init_out, CTOR)
        self.assertIn(CTOR.replace("\t, mCount( 3 )\n", "").replace("{\n\tmSpeed", "{\n\tmCount = 3;\n\tmSpeed"), out)
        self.assertFalse(any("Super =" in t for t in out))   # the base class stays

    def test_value_initialized(self):
        out = texts(permute.t_init_value, CTOR)
        self.assertIn(CTOR.replace("mOffset( ForceInitToZero )", "mOffset()"), out)
        self.assertIn("FFoo::FFoo()\n\t: mPair()\n{\n}", texts(permute.t_init_value, "FFoo::FFoo()\n\t: mPair{ 0, 0 }\n{\n}"))
        self.assertIn("FFoo::FFoo()\n\t: mPair{}\n{\n}", texts(permute.t_init_value, "FFoo::FFoo()\n\t: mPair()\n{\n}"))

    def test_members_the_init_list_lacks_are_named_there(self):
        members = permute.class_members(HEADER, "AWidgetHolder")
        out = texts(permute.t_init_add, CTOR, members=members)
        self.assertIn(CTOR.replace("\t, mCount( 3 )\n", "\t, mSlots()\n\t, mCount( 3 )\n"), out)
        self.assertIn(CTOR.replace("\t, mCount( 3 )\n", "\t, mCount( 3 )\n\t, mOwner( nullptr )\n"), out)
        everything = [t for t in out if "mSlots()" in t and "mOwner" in t]
        self.assertTrue(everything)
        self.assertFalse(any("mSpeed(" in t or "bReady(" in t or "Label(" in t for t in out))   # initialized, assigned
        self.assertEqual(texts(permute.t_init_add, LOOP, members=members), [])   # not a constructor

    def test_a_constructor_without_an_init_list_gets_one(self):
        ctor = "FFoo::FFoo()\n{\n\tmA = 1;\n}"
        self.assertIn("FFoo::FFoo()\n\t: mA( 1 )\n{\n}", texts(permute.t_init_in, ctor))


class LiteralTest(unittest.TestCase):
    def test_fname_explicit_and_implicit(self):
        out = texts(permute.t_fname, CTOR)
        self.assertIn(CTOR.replace('FName( "IgnoreMe" )', '"IgnoreMe"'), out)
        back = texts(permute.t_fname, CTOR.replace('FName( "IgnoreMe" )', '"IgnoreMe"'))
        self.assertIn(CTOR, back)
        self.assertEqual(texts(permute.t_fname, LOOP), [])   # a literal inside TEXT() stays

    def test_fstring_temporaries(self):
        out = texts(permute.t_fstring, CTOR)
        self.assertIn(CTOR.replace('FString( TEXT( "Holder" ) )', 'TEXT( "Holder" )'), out)
        out = texts(permute.t_fstring, LOOP)
        self.assertIn(LOOP.replace('TEXT( "Cleaned " ) + Name', 'FString( TEXT( "Cleaned " ) ) + Name'), out)


class LoopTest(unittest.TestCase):
    def test_loop_variable_by_reference_and_by_value(self):
        out = texts(permute.t_loop_reference, LOOP)
        self.assertIn(LOOP.replace("for( IThing* thing :", "for( IThing*& thing :"), out)
        self.assertIn(LOOP.replace("for( const FBar& bar :", "for( const FBar bar :"), out)
        self.assertIn(LOOP.replace("for( const FBar& bar :", "for( FBar bar :"), out)

    def test_counter_types(self):
        out = texts(permute.t_counter_type, LOOP)
        for t in ("uint64", "int64", "uint32", "int"):
            self.assertIn(LOOP.replace("for( int32 i = 0;", f"for( {t} i = 0;"), out)
        self.assertEqual(len(out), 4)


class OrderTest(unittest.TestCase):
    def test_declarations_moved_to_every_place_they_can_go(self):
        out = texts(permute.t_move_declaration, LOOP)
        up = LOOP.replace("\tbool bAny = removed > 0;\n\tLog( removed );\n", "\tLog( removed );\n\tbool bAny = removed > 0;\n")
        self.assertIn(up, out)
        for t in out:   # never below a use, never above what it reads
            self.assertLess(t.index("int32 removed = 0;"), t.index("removed +="))
            self.assertLess(t.index("bool bAny"), t.index("if( bAny"))
            self.assertLess(t.index("for( int32 i"), t.index("bool bAny"))   # the loop changes removed

    def test_a_return_is_not_a_declaration(self):
        body = "void F()\n{\n\tif( a )\n\t{\n\t\tClear();\n\t\treturn true;\n\t}\n}"
        self.assertEqual(texts(permute.t_move_declaration, body), [])

    def test_a_test_hoisted_into_a_local_and_inlined_back(self):
        out = texts(permute.t_hoist_test, LOOP)
        self.assertIn(LOOP.replace("\tif( GetWorld()->IsGame() )", "\tconst bool bTest = GetWorld()->IsGame();\n\tif( bTest )"), out)
        inlined = texts(permute.t_inline_local, LOOP)
        self.assertIn(LOOP.replace("\tbool bAny = removed > 0;\n", "").replace("if( bAny || mForce )", "if( ( removed > 0 ) || mForce )"), inlined)

    def test_arguments_swapped_one_pair_at_a_time(self):
        out = texts(permute.t_swap_arguments, LOOP)
        self.assertIn(LOOP.replace("Notify( mOwner, removed, 2 )", "Notify( removed, mOwner, 2 )"), out)
        self.assertIn(LOOP.replace("Notify( mOwner, removed, 2 )", "Notify( mOwner, 2, removed )"), out)
        calls = "void F()\n{\n\tGo( Make(), b );\n}"
        self.assertEqual(texts(permute.t_swap_arguments, calls), [])   # a call as argument: its side effects

    def test_terms_and_operands_swapped(self):
        out = texts(permute.t_swap_terms, LOOP)
        self.assertIn(LOOP.replace("if( bAny || mForce )", "if( mForce || bAny )"), out)

    def test_statements_swapped_one_pair_at_a_time(self):
        out = texts(permute.t_swap_statements, LOOP)
        self.assertIn(LOOP.replace("\tbool bAny = removed > 0;\n\tLog( removed );\n", "\tLog( removed );\n\tbool bAny = removed > 0;\n"), out)
        for t in out:
            self.assertLess(t.index("const FString msg"), t.index("Send( msg )"))


class TargetedTest(unittest.TestCase):
    def test_every_candidate_once_rule_by_rule_with_what_changed(self):
        members = permute.class_members(HEADER, "AWidgetHolder")
        edits = permute.targeted(CTOR, members=members)
        self.assertEqual(len({e.text for e in edits}), len(edits))
        self.assertTrue(all(e.text != CTOR for e in edits))
        rules = [e.rule for e in edits]
        self.assertEqual(rules, sorted(rules, key=[r.__name__[2:].replace("_", "-") for r in permute.TARGETED].index))
        e = next(e for e in edits if e.rule == "fname")
        self.assertIn('"IgnoreMe"', e.change)

    def test_the_same_change_has_the_same_signature_on_another_base(self):
        a = permute.targeted(LOOP)
        base2 = LOOP.replace("Go();", "Go( 1 );")
        b = permute.targeted(base2)
        sa = {e.change for e in a if e.rule == "counter-type"}
        sb = {e.change for e in b if e.rule == "counter-type"}
        self.assertEqual(sa, sb)


class KeepLinesTest(unittest.TestCase):
    def test_a_longer_function_gives_back_blank_lines(self):
        old = "void F()\n{\n\tA();\n\n\tB();\n\n\n\tC();\n}"
        new = "void F()\n{\n\tconst bool b = A();\n\tif( b ) {}\n\n\tB();\n\n\n\tC();\n}"
        kept = permute.keep_lines(old, new)
        self.assertEqual(kept.count("\n"), old.count("\n"))
        self.assertTrue(kept.endswith("\tC();\n}"))
        self.assertEqual(kept.split("\n").index("\tC();"), old.split("\n").index("\tC();"))

    def test_a_shorter_function_gets_blank_lines_where_it_shrank(self):
        old = "void F()\n{\n\tint a = 1;\n\tUse( a );\n\tC();\n}"
        new = "void F()\n{\n\tUse( 1 );\n\tC();\n}"
        kept = permute.keep_lines(old, new)
        self.assertEqual(kept, "void F()\n{\n\tUse( 1 );\n\n\tC();\n}")


if __name__ == "__main__":
    unittest.main()
