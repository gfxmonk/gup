from util import *

class TestBasicRules(TestCase):
	def setUp(self):
		super(TestBasicRules, self).setUp()
		self.write("default.gup", echo_to_target('$2'))
		self.source_contents = "Don't overwrite me!"
		self.write("source.txt", self.source_contents)

	def test_doesnt_overwrite_existing_file(self):
		self.assertRaises(Unbuildable, lambda: self.build("source.txt"))

		self.build_u("source.txt")
		self.assertEqual(self.read("source.txt"), self.source_contents)
	
	def test_only_creates_new_files_matching_pattern(self):
		self.assertRaises(Unbuildable, lambda: self.build("output.txt"))

		self.write("Gupfile", "default.gup:\n\toutput.txt\n\tfoo.txt")
		self.build("output.txt")
		self.build("foo.txt")
		self.assertEqual(self.read("output.txt"), "output.txt")

		self.write("Gupfile", "default.gup:\n\tf*.txt")
		self.assertRaises(Unbuildable, lambda: self.build("output.txt"))
		self.build("foo.txt")
		self.build("far.txt")

	def test_exclusions(self):
		self.write("Gupfile", "default.gup:\n\t*.txt\n\n\t!source.txt")
		self.build("output.txt")
		self.assertRaises(Unbuildable, lambda: self.build("source.txt"))
		self.assertEqual(self.read("source.txt"), self.source_contents)

class TestGupdirectory(TestCase):
	def test_gupdir_is_search_target(self):
		self.write("gup/base.gup", '#!/bin/bash\necho -n "base" > "$1"')
		self.build('base')
		self.assertEqual(self.read('base'), 'base')
	
	def test_multiple_gup_dirs_searched(self):
		self.write("a/gup/b/c.gup", echo_to_target('c'))
		# shadowed by the above rule
		self.write("gup/a/b/c.gup", echo_to_target('wrong c'))

		self.write("gup/a/b/d.gup", echo_to_target('d'))

		self.build_assert('a/b/c', 'c')
		self.build_assert('a/b/d', 'd')

	def test_patterns_match_against_path_from_gupfile(self):
		self.write("a/default.gup", echo_to_target('ok'))
		self.write("a/Gupfile", 'default.gup:\n\tb/*/d')

		self.build_assert('a/b/c/d', 'ok')
		self.build_assert('a/b/xyz/d', 'ok')
		self.assertRaises(Unbuildable, lambda: self.build("x/b/cd"))
		self.assertEquals(os.listdir(self.ROOT), ['a'])

	def test_patterns_match_against_path_from_gupfile_ignoring(self):
		self.write("a/default.gup", echo_to_target('ok'))
		self.write("a/Gupfile", 'default.gup:\n\tb/*/d')

		self.build_assert('a/b/c/d', 'ok')
		self.build_assert('a/b/xyz/d', 'ok')
		self.assertRaises(Unbuildable, lambda: self.build("x/b/cd"))
		self.assertEquals(os.listdir(self.ROOT), ['a'])
