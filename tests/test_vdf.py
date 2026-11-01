import unittest

from portkit import vdf

MANIFEST = r'''"AppState"
{
	"appid"		"526870"
	"name"		"Satisfactory"
	// a comment
	"InstalledDepots"
	{
		"526871"	{ "manifest" "123" }
	}
	"path"		"Z:\\Volumes\\SMART"
}
'''


class VDFTest(unittest.TestCase):
    def test_parse(self):
        d = vdf.loads(MANIFEST)["AppState"]
        self.assertEqual(d["appid"], "526870")
        self.assertEqual(d["InstalledDepots"]["526871"]["manifest"], "123")
        self.assertEqual(d["path"], "Z:\\Volumes\\SMART")

    def test_round_trip(self):
        d = vdf.loads(MANIFEST)
        self.assertEqual(vdf.loads(vdf.dumps(d)), d)

    def test_unbalanced(self):
        with self.assertRaises(ValueError):
            vdf.loads('"a" { "b" "c"')


if __name__ == "__main__":
    unittest.main()
