from __future__ import print_function
import os
import sys
from . import builder
from .log import getLogger
from .util import get_mtime
from .state import FileDependency, TargetState
from .error import Unbuildable, TargetFailed, SafeError
from . import var

log = getLogger('gup.cmd') # hard-coded in case of __main__

class Task(object):
	'''
	Each target we're asked to build is represented as a Task,
	so that they can be invoked in parallel
	'''
	def __init__(self, opts, parent_target, target_path):
		self.target_path = target_path
		self.opts = opts
		self.parent_target = parent_target
	
	def prepare(self):
		target_path = self.target_path
		opts = self.opts

		target = self.target = builder.prepare_build(target_path)
		if target is None:
			if opts.ifcreate:
				if self.parent_target is None:
					log.warn("--ifcreate was used outside of a gup target")
				return None
			if opts.update and os.path.exists(target_path):
				self.report_nobuild()
				return None
			raise Unbuildable("Don't know how to build %s" % (target_path))
		return target

	def build(self):
		'''
		run in a child process
		'''
		self.built = self.target.build(update=self.opts.update)
		self.complete()

	def complete(self):
		if self.parent_target is not None:
			target_path = self.target_path
			mtime = get_mtime(target_path)
			relpath = os.path.relpath(os.path.abspath(target_path), os.path.dirname(self.parent_target))

			checksum = None
			if self.target:
				deps = self.target.state.deps()
				if deps:
					checksum = deps.checksum

			dep = FileDependency(mtime=mtime, path=relpath, checksum=checksum)
			TargetState(self.parent_target).add_dependency(dep)
	
	def handle_result(self, _, rv):
		log.debug("build process exited with status: %r" % (rv,))
		if rv == 0:
			return
		if rv == SafeError.exitcode:
			# already logged - just raise an empty exception to propagate exit code
			raise SafeError(None)
		else:
			raise RuntimeError("unknown error in child process - exit status %s" % rv)

	def report_nobuild(self):
		if var.IS_ROOT:
			log.info("%s: up to date", self.target_path)
		else:
			log.debug("%s: up to date", self.target_path)


class TaskRunner(object):
	def __init__(self):
		self.tasks = []

	def add(self, fn):
		self.tasks.append(fn)

	def run(self):
		from . import jwack
		while self.tasks:
			task = self.tasks.pop(0)
			log.debug("START job")
			jwack.start_job('TODO', task.build, task.handle_result)
			log.debug("job running in bg...")
		jwack.wait_all()
	
