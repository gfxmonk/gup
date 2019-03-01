open Batteries
open Std
open Lwt

let log = Logging.get_logger "gup.par"
module ExtUnix = ExtUnix.Specific

(* because lockf is pretty much insane,
 * we must *never* close an FD that we might hold a lock
 * on elsewhere in the same process.
 *
 * So as well as lockf() based locking, we have a
 * process-wide register of locks (keyed by device_id,inode).
 *
 * To take a lock, you first need to hold the (exclusive)
 * process-level lock for that file's identity
 *
 * TODO: only use this if we're actually using --jobs=n for n>1
 *)
module FileIdentity = struct
	type device_id = | Device of int
	type inode = | Inode of int
	type t = (device_id * inode)
	let _extract (Device dev, Inode ino) = (dev, ino)
	let create stats = (Device stats.Unix.st_dev, Inode stats.Unix.st_ino)
	let compare a b =
		Tuple2.compare ~cmp1:Int.compare ~cmp2:Int.compare (_extract a) (_extract b)
end

module LockMap = struct
	include Map.Make (FileIdentity)
	let _map = ref empty
	let with_lock id fn =
		let lock =
			try find id !_map
			with Not_found -> (
				let lock = Lwt_mutex.create () in
				_map := add id lock !_map;
				lock
			)
		in
		Lwt_mutex.with_lock lock fn
end

type fds = (Lwt_unix.file_descr * Lwt_unix.file_descr) option ref

let _lwt_descriptors (r,w) = (
	Lwt_unix.of_unix_file_descr ~blocking:true ~set_flags:false r,
	Lwt_unix.of_unix_file_descr ~blocking:true ~set_flags:false w
)

class fd_jobserver (read_end, write_end) toplevel =
	let _have_token = Lwt_condition.create () in
	let token = 't' in

	(* initial token held by this process *)
	let _mytoken = ref (Some ()) in
	let repeat_tokens len = Bytes.make len token in

	(* for debugging only *)
	let _free_tokens = ref 0 in

	let _write_tokens n =
		let buf = repeat_tokens n in
		let%lwt written = Lwt_unix.write write_end buf 0 n in
		assert (written = n);
		Lwt.return_unit
	in

	let _read_token () =
		let buf = Bytes.make 1 ' ' in
		let success = ref false in
		while%lwt not !success do
			(* XXX does this really return without reading sometimes? *)
			let%lwt n = Lwt_unix.read read_end buf 0 1 in
			let succ = n > 0 in
			success := succ;
			if not succ
			then Lwt_unix.wait_read read_end
			else Lwt.return_unit
		done
	in

	let _release n =
		log#trace "release(%d)" n;
		let n = match !_mytoken with
		| Some _ -> n
		| None ->
				(* keep one for myself *)
				_mytoken := Some ();
				Lwt_condition.signal _have_token ();
				n - 1
		in
		if n > 0 then (
			_free_tokens := !_free_tokens + n;
			log#trace "free tokens: %d" !_free_tokens;
			_write_tokens n
		) else Lwt.return_unit
	in

	let _get_token () =
		(* Get (and consume) a single token *)
		let use_mine = fun () ->
			assert (Option.is_some !_mytoken);
			_mytoken := None;
			log#trace "used my own token";
			Lwt.return_unit
		in

		match !_mytoken with
			| Some t -> use_mine ()
			| None ->
				log#trace "waiting for token...";
				let%lwt () = Lwt.pick [
					Lwt_condition.wait _have_token >>= use_mine;
					_read_token () >>= fun () ->
						_free_tokens := !_free_tokens - 1;
						log#trace "used a free token, there are %d left" !_free_tokens;
						Lwt.return_unit;
				] in
				log#trace "got a token";
				Lwt.return_unit
	in

	let () = Option.may (fun tokens -> Lwt_main.run (_release (tokens - 1))) toplevel in

object (self)
	method finish =
		match toplevel with
			| Some tokens ->
				(* wait for outstanding tasks by comsuming the number of tokens we started with *)
				let remaining = ref (tokens - 1) in
				let buf = repeat_tokens !remaining in
				while%lwt !remaining > 0 do
					log#debug "waiting for %d free tokens to be returned" !remaining;
					let%lwt n = Lwt_unix.read read_end buf 0 !remaining in
					remaining := !remaining - n;
					Lwt.return_unit
				done
			| None -> Lwt.return_unit

	method run_job : 'a. (unit -> 'a Lwt.t) -> 'a Lwt.t = fun fn ->
		let%lwt () = _get_token () in
		(try%lwt
			fn ()
		with e -> raise e)
		[%lwt.finally
			_release 1
		]

	method with_process_mutex : 'a. Lwt_unix.file_descr -> (unit -> 'a Lwt.t) -> 'a Lwt.t =
	fun fd fn ->
		let%lwt stats = Lwt_unix.fstat fd in
		LockMap.with_lock (FileIdentity.create stats) fn
end

class named_jobserver path toplevel =
	let fds =
		log#trace "opening jobserver at %s" path;
		let perm = 0o000 in (* ignored, since we don't use O_CREAT *)
		let r = Unix.openfile path [Unix.O_RDONLY ; Unix.O_NONBLOCK ; Unix.O_CLOEXEC] perm in
		let w = Unix.openfile path [Unix.O_WRONLY; Unix.O_CLOEXEC] perm in
		Unix.clear_nonblock r;
		(_lwt_descriptors (r,w))
	in

	let server = new fd_jobserver fds toplevel in

object (self)
	method finish =
		let%lwt () = server#finish in
		let (r, w) = fds in
		let%lwt (_:unit list) = Lwt_list.map_p Lwt_unix.close [r;w] in

		(* delete jobserver file if we are the toplevel *)
		let%lwt () = Lwt_option.may (fun _ -> Lwt_unix.unlink path) toplevel in
		Lwt.return_unit

	method run_job : 'a. (unit -> 'a Lwt.t) -> 'a Lwt.t = fun fn ->
		server#run_job fn

	method with_process_mutex : 'a. Lwt_unix.file_descr -> (unit -> 'a Lwt.t) -> 'a Lwt.t =
	fun fd fn -> server#with_process_mutex fd fn
end

class serial_jobserver =
	let lock = Lwt_mutex.create () in
object (self)
	method run_job : 'a. (unit -> 'a Lwt.t) -> 'a Lwt.t = fun fn ->
		Lwt_mutex.with_lock lock fn

	method finish = Lwt.return_unit
	method with_process_mutex : 'a. Lwt_unix.file_descr -> (unit -> 'a Lwt.t) -> 'a Lwt.t =
	fun fd fn -> ignore fd; fn ()
end

module Jobserver = struct
	let _inherited_vars = ref []
	let _impl = ref (new serial_jobserver)

	let makeflags_var = "MAKEFLAGS"
	let jobserver_var = "GUP_JOBSERVER"
	let not_required = "0"

	let _extract_fds makeflags =
		let flags_re = Str.regexp "--jobserver-\\(auth\\|fds\\)=\\([0-9]+\\),\\([0-9]+\\)" in
		try
			ignore @@ Str.search_forward flags_re makeflags 0;
			Some (
				Int.of_string (Str.matched_group 2 makeflags),
				Int.of_string (Str.matched_group 3 makeflags)
			)
		with Not_found -> None


	let _discover_jobserver () = (
		(* open GUP_JOBSERVER if present *)
		let inherit_named_jobserver path = new named_jobserver path None in
		let server = Var.get jobserver_var |> Option.map inherit_named_jobserver in
		begin match server with
			| None -> (
				(* try extracting from MAKEFLAGS, if present *)
				let fd_ints = Option.bind (Var.get makeflags_var) _extract_fds in

				Option.bind fd_ints (fun (r,w) ->
					let r = ExtUnix.file_descr_of_int r
					and w = ExtUnix.file_descr_of_int w
					in
					(* check validity of fds given in $MAKEFLAGS *)
					let valid fd = ExtUnix.is_open_descr fd in
					if valid r && valid w then (
						log#trace "using fds %a"
							(Tuple.Tuple2.print Int.print Int.print) (Option.get fd_ints);

						Some (new fd_jobserver (_lwt_descriptors (r,w)) None)
					) else (
						log#warn (
							"broken --jobserver-fds in $MAKEFLAGS;" ^^
							"prefix your Makefile rule with '+'\n" ^^
							"or pass --jobs flag to gup directly to ignore make's jobserver\n" ^^
							"Assuming --jobs=1");
						ExtUnix.unsetenv makeflags_var;
						None
					)
				)
			)
			| server -> server
		end
	)

	let _create_named_pipe ():string = (
		let filename = Filename.concat
			(Filename.get_temp_dir_name ())
			("gup-job-" ^ (string_of_int @@ Unix.getpid ())) in

		let create = fun () ->
			Unix.mkfifo filename 0o600
		in
		(* if pipe already exists it must be old, so remove it *)
		begin try create ()
		with Unix.Unix_error (Unix.EEXIST, _, _) -> (
			log#warn "removing stale jobserver file: %s" filename;
			Unix.unlink filename;
			create ()
		) end;
		log#trace "created jobserver at %s" filename;
		filename
	)

	let extend_env env =
		!_inherited_vars |> List.fold_left (fun env (key, value) ->
			EnvironmentMap.add key value env
		) env

	let setup maxjobs fn = (
		(* run the job server *)
		let inherited = ref None in

		if (Var.get jobserver_var) <> (Some not_required) then (
			if Option.is_none maxjobs then begin
				(* no --jobs param given, check for a running jobserver *)
				inherited := _discover_jobserver ()
			end;

			begin match !inherited with
				| Some server -> _impl := server
				| None -> (
					(* no jobserver set, start our own *)
					let maxjobs = Option.default 1 maxjobs in
					if maxjobs = 1 then (
						log#debug "no need for a jobserver (--jobs=1)";
						_inherited_vars := (jobserver_var, not_required) :: !_inherited_vars;
					) else (
						assert (maxjobs > 0);
						(* need to start a new server *)
						log#trace "new jobserver! %d" maxjobs;

						let path = _create_named_pipe () in
						_inherited_vars := (jobserver_var, path) :: !_inherited_vars;
						_impl := new named_jobserver path (Some maxjobs)
					)
				)
			end
		);

		(
			try%lwt
				fn ()
			with e -> raise e
		)[%lwt.finally 
			!_impl#finish
		]
	)

	let run_job fn = !_impl#run_job fn
	let with_process_mutex fd fn = !_impl#with_process_mutex fd fn
end


(***
 * Lock files
 *)
type lock_mode =
	| ReadLock
	| WriteLock

let lock_flag mode = match mode with
	| ReadLock -> Unix.F_RLOCK
	| WriteLock -> Unix.F_LOCK

let lock_flag_nb mode = match mode with
	| ReadLock -> Unix.F_TRLOCK
	| WriteLock -> Unix.F_TLOCK

let print_lock_mode out mode = Printf.fprintf out "%s" (match mode with
	| ReadLock -> "ReadLock"
	| WriteLock -> "WriteLock"
)

type active_lock = (lock_mode * Lwt_unix.file_descr * string)

type lock_state =
	| Unlocked
	| Locked of active_lock
	| PendingLock of active_lock Lwt.t Lazy.t

exception Not_locked


(* a reentrant lock file *)
class lock_file ~target lock_path =
	let current_lock = ref None in

	let do_lockf path fd flag =
		try%lwt
			Lwt_unix.lockf fd flag 0
		with Unix.Unix_error (errno, _,_) ->
			Error.raise_safe "Unable to lock file %s: %s" path (Unix.error_message errno)
	in

	let with_lock mode path f =
		(* lock file *)
		let%lwt fd = Lwt_unix.openfile path [Unix.O_RDWR; Unix.O_CREAT; Unix.O_CLOEXEC] 0o664 in
		
		(* ensure only one instance process-wide ever locks the given inode *)
		
		Jobserver.with_process_mutex fd (fun () ->
			let%lwt () = do_lockf path fd (lock_flag mode) in
			log#trace "--Lock[%s] %a" path print_lock_mode mode;
			(
				try%lwt
					f ()
				with e -> raise e
			) [%lwt.finally (
				log#trace "Unlock[%s]" path;
				Lwt_unix.close fd
			)]
		)
	in

object (self)
	method use : 'a. lock_mode -> (string -> 'a Lwt.t) -> 'a Lwt.t = fun mode f ->
		match !current_lock with

			(* acquire initial lock *)
			| None -> with_lock mode lock_path (fun () ->
					current_lock := Some mode;
					let rv = f target in
					current_lock := None;
					rv
				)

			(* already locked, perform action immediately *)
			| Some WriteLock -> f target

			(* other transitions not yet needed *)
			| _ -> assert false
end

