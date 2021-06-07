from __future__ import print_function
from mocktest import *
import mocktest
import os
import re
import sys
import stat
import time
import tempfile
import contextlib
import subprocess
import logging
import unittest
import itertools
from datetime import datetime, timedelta

from gup.error import *
from gup.util import rmtree
from gup.log import TRACE_LVL

logging.basicConfig(level=TRACE_LVL)
log = logging.getLogger('TEST')

def expand_exe(exe):
	if not os.path.isabs(exe):
		from whichcraft import which
		exe = which(exe)
	return exe

os.environ['OCAMLRUNPARAM'] = 'b' # make ocaml print backtraces

GUP_EXES = os.environ.get('GUP_EXE', 'gup').split(os.pathsep)
print(repr(GUP_EXES), file=sys.stderr)
LAME_MTIME = sys.platform == 'darwin'
IS_WINDOWS = sys.platform == 'win32'
if IS_WINDOWS: GUP_EXES = [e + '.cmd' for e in GUP_EXES]
IS_OCAML = all([os.path.join("ocaml", "bin") in expand_exe(exe) for exe in GUP_EXES])
initial_exes = iter(itertools.repeat(GUP_EXES[0]))

def mkdirp(p):
	if not os.path.exists(p):
		os.makedirs(p)

BASH = '#!bash\nset -eux\n'
def echo_to_target(contents):
	return BASH + 'echo -n "%s" > "$1"' % (contents,)

def echo_file_contents(dep):
	return BASH + 'gup -u "%s"; cat "%s" > "$1"' % (dep, dep)

_skip_permutation_sentinel = object()
def skipPermutations(fn):
	fn.skip_permutations = _skip_permutation_sentinel
	return fn

def has_feature(name):
	return all([name in _build(exe, args=['--features'], cwd=None) for exe in GUP_EXES])

def _build(exe, args, cwd, env=None, include_logging=False, throwing=True):
	env = env or os.environ
	log.warn("\n\nRunning %s with args: %r [cwd=%r]" % (exe, list(args), cwd))
	env = env.copy()
	for key in list(env.keys()):
		# clear out any gup state
		if key == 'MAKEFLAGS' or key.startswith('GUP_'):
			del env[key]

	env['GUP_IN_TESTS'] = '1'
	use_color = sys.stdout.isatty() and not IS_WINDOWS
	env['GUP_COLOR'] = '1' if use_color else '0'

	exe_dir = os.path.basename(os.path.dirname(os.path.dirname(exe)))
	exe_args = [exe]
	if exe_dir == 'ocaml':
		pass
	elif exe_dir == 'python':
		if not IS_WINDOWS:
			exe_args = [sys.executable, exe]
	elif exe == 'gup':
		# unit tests, just use what's on $PATH
		pass
	else:
		raise RuntimeError("Unknown exe_dir: %r" % exe_dir)

	proc = subprocess.Popen(exe_args + list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

	child_log = logging.getLogger('out')
	err = None
	lines = []
	while True:
		line = proc.stdout.readline()
		if not line:
				break
		line = line.decode('utf-8').rstrip()
		if include_logging or not line.startswith('#'):
			lines.append(line)
		unbuildable_msg = "Don't know how to build"
		unbuildable_idx = line.find(unbuildable_msg)
		if unbuildable_idx != -1:
			err = Unbuildable(line[unbuildable_idx + len(unbuildable_msg) + 1:])
		child_log.info(line)

	returncode = proc.wait()
	if returncode != 0:
		if err is None:
			err = SafeError('gup failed with status %d' % returncode)
		if throwing:
			raise err
	if not throwing:
		return (returncode, lines)
	return lines

class TestCase(mocktest.TestCase):
	exes = None

	def setUp(self):
		log.error("START")
		super(TestCase, self).setUp()
		self.invocation_count = 0
		if self.exes is None:
			self.exes = initial_exes
		self.ROOT = os.path.realpath(tempfile.mkdtemp(prefix='gup-test-'))
		log.debug('root: %s', self.ROOT)
		# self._last_build_time = None
	
	def path(self, *p):
		return os.path.join(self.ROOT, *p)

	def write(self, p, contents):
		p = self.path(p)
		mkdirp(os.path.dirname(p))
		with open(p, 'w') as f:
			f.write(contents)

	def write_executable(self, p, contents):
		self.write(p, contents)
		self.make_executable(p)

	def read(self, p):
		with open(self.path(p)) as f:
			return f.read().strip()
	
	def _teardown(self):
		rmtree(self.ROOT)
		self.exes = None

	def tearDown(self):
		self._teardown();
		super(TestCase, self).tearDown()

		## permutations
		if len(GUP_EXES) > 1 and self.invocation_count > 1:
			test_method = getattr(self, self._testMethodName)
			if getattr(test_method, 'skip_permutations', None) is _skip_permutation_sentinel:
				return

			log.debug("running permutations based on %s invocations ..." % self.invocation_count)
			print("", file=sys.stderr)
			gup_exe_sequences = list(itertools.product(GUP_EXES, repeat=self.invocation_count))
			for sequence in gup_exe_sequences:
				if all([exe == GUP_EXES[0] for exe in sequence]):
					# this is what the first test run does; skip it
					continue
				permutation_desc = [path.split(os.path.sep)[-3] for path in sequence]
				print("-- PERMUTATION: %r" % (permutation_desc,), file=sys.stderr)
				log.info("\n\n-----------------------\nPERMUTATION: %r", permutation_desc)
				self.exes = iter(sequence)
				self.setUp()
				try:
					test_method()
				finally:
					self._teardown()
					#XXX hack
					mocktest._teardown()


	@contextlib.contextmanager
	def _root_cwd(self):
		with(self.in_dir(self.ROOT)):
			yield

	@contextlib.contextmanager
	def in_dir(self, dir):
		initial = os.getcwd()
		try:
			os.chdir(dir)
			yield
		finally:
			os.chdir(initial)

	def _build(self, args, cwd=None, last=False, **k):
		self.invocation_count += 1
		log.debug("invocation count = %r", self.invocation_count)

		with self._root_cwd():
			exe = next(self.exes)
			lines = _build(exe, args=args, cwd=cwd, **k)

		if LAME_MTIME and not last:
			# OSX has 1-second mtime resolution.
			# After each build, we sleep just over 1s to make sure
			# any further modifications can't be in the same second

			# tests can pass `last=True` to avoid this, on the
			# condition that they won't rebuild this target
			time.sleep(1.1)

		return lines

	def build(self, *targets, **k):
		return self._build(targets, **k)

	def mtime(self, p):
		mtime = os.lstat(os.path.join(self.ROOT, p)).st_mtime
		logging.debug("mtime for %s is %s" % (p, mtime))
		return mtime

	def build_u(self, *targets, **k):
		return self._build(['--update'] + list(targets), **k)
	
	def build_assert(self, target, contents, **k):
		self.build(target, **k)
		self.assertEqual(self.read(target), contents)

	def build_u_assert(self, target, contents):
		self.build(target)
		self.assertEqual(self.read(target), contents)
	
	def touch(self, target):
		path = self.path(target)
		self.mkdirp(os.path.dirname(path))
		with open(path, 'a'):
			os.utime(path, None)

	def listdir(self, dir='.'):
		return sorted(filter(lambda f: not f.startswith('.'), os.listdir(self.path(dir))))
	
	def completionTargets(self, dir=None):
		args = ['--targets']
		if dir is not None: args.append(dir)
		return sorted(self._build(args))

	def assertRebuilds(self, target, fn, built=False, mtime_file=None, **kw):
		# mtime_file allows you to build `foo` but check that `bar` has been modified (mostly useful in symlink cases)
		if mtime_file is None: mtime_file=target
		if not built: self.build_u(target, **kw)
		mtime = self.mtime(mtime_file)
		fn()
		rv = self.build_u(target, **kw)
		self.assertNotEqual(self.mtime(mtime_file), mtime, "target %s didn't get rebuilt" % (target,))
		return rv
	
	def assertDuration(self, min, max, fn):
		initial_time = datetime.now()
		rv = fn()

		elapsed_time = (datetime.now() - initial_time).total_seconds()
		assert elapsed_time <= max, "elapsed time > %ss (%s)" % (max, elapsed_time)
		assert elapsed_time >= min, "elapsed time < %ss (%s)" % (min, elapsed_time)
		return rv

	def assertNotRebuilds(self, target, fn, built=False, mtime_file=None):
		if mtime_file is None: mtime_file = target
		if not built: self.build_u(target)
		mtime = self.mtime(mtime_file)
		fn()
		self.build_u(target)
		self.assertEqual(self.mtime(mtime_file), mtime, "target %s got rebuilt" % (target,))

	def buildErrors(self, target):
		status, output = self.build(target, throwing = False, include_logging = True)
		self.assertEqual(status, 2)
		error_prefix="# ERROR "
		return [line[len(error_prefix):].strip() for line in output if line.startswith(error_prefix)]
	
	def rename(self, src, dest):
		os.rename(self.path(src), self.path(dest))

	def unlink(self, src):
		os.unlink(self.path(src))

	def symlink(self, target, src, force=False):
		src = self.path(src)
		if force and os.path.exists(src):
			os.unlink(src)
		os.symlink(target, src)

	def mkdirp(self, p):
		mkdirp(self.path(p))
	
	def exists(self, p):
		return os.path.exists(self.path(p))

	def lexists(self, p):
		return os.path.lexists(self.path(p))

	def make_executable(self, p):
		st = os.stat(self.path(p))
		os.chmod(self.path(p), st.st_mode | stat.S_IXUSR)

