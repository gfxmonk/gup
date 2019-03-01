open Batteries
open OUnit2
open Gup.Parallel

let print_opt_int_pair p =
	Printf.sprintf2 "%a" (Option.print (Tuple.Tuple2.print Int.print Int.print)) p

let assertFds expected str =
	assert_equal ~printer: print_opt_int_pair expected (Jobserver._extract_fds str)

let suite = "MAKEFLAGS" >:::
[
	"extracts FDs from --jobserver-fds" >:: (fun _ ->
		assertFds (Some (1,2)) "--jobserver-fds=1,2";
		assertFds (Some (100,200)) "make --jobserver-fds=100,200 -j";
	);

	"extracts FDs from --jobserver-auth" >:: (fun _ ->
		assertFds (Some (1,2)) "--jobserver-auth=1,2";
		assertFds (Some (100,200)) "make --jobserver-auth=100,200 -j";
	);
]
