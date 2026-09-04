" Halis syntax highlighting — Vim/Neovim syntax file.
" Stage 14 release.

if exists('b:current_syntax')
  finish
endif

" Keywords.
syn keyword halisKeyword fn let mut struct impl enum import uses pure extern
syn keyword halisKeyword requires ensures
syn keyword halisKeyword if else while for in return break continue match

" Booleans.
syn keyword halisBoolean true false

" Built-in types.
syn keyword halisType int float bool str void
syn keyword halisType list map tainted Chan Task

" Effects (treated as types for coloring).
syn keyword halisEffect IO Fs Clock Args Exit Net Rand Proc Conc

" Built-in functions (common ones).
syn keyword halisBuiltin println print len str int panic clock_ms args exit
syn keyword halisBuiltin chr range map_new drop clone take file_exists
syn keyword halisBuiltin read_file write_file tainted_args taint_mark
syn keyword halisBuiltin taint_unwrap read_file_tainted read_line
syn keyword halisBuiltin net_lookup rand_int rand_float rand_seed proc_exec
syn keyword halisBuiltin spawn chan_new chan_new_bounded select
syn keyword halisBuiltin try_send recv_or

" Comments.
syn match halisComment "#.*$" contains=halisTodo
syn keyword halisTodo TODO FIXME XXX BUG HACK contained

" Strings.
" SPEC \u00a72: HLS only supports \n \t \\ \" escapes. Any other
" escape is a syntax error. Highlight the valid set as escapes; let
" anything else fall through as a normal string char (the compiler
" will reject it, but the editor should not paint it as a valid
" escape — that would mislead the user).
syn region halisString start=+"+ end=+"+ contains=halisEscape
syn match halisEscape +\\[nrt"\\]+ contained

" Numbers (HLS supports _ as a digit-group separator).
syn match halisFloat "\d\(_\?\d\)*\.\d\(_\?\d\)*\([eE][-+]\?\d\(_\?\d\)*\)\?"
syn match halisInt "\d\(_\?\d\)*"

" Operators.
syn match halisOperator "[-+*/%<>=!&|?:.,]"
syn match halisArrow "->"
syn match halisArrow "=>"

" Highlighting links.
hi def link halisKeyword Keyword
hi def link halisBoolean Boolean
hi def link halisType Type
hi def link halisEffect Special
hi def link halisBuiltin Function
hi def link halisComment Comment
hi def link halisTodo Todo
hi def link halisString String
hi def link halisEscape SpecialChar
hi def link halisFloat Float
hi def link halisInt Number
hi def link halisOperator Operator
hi def link halisArrow SpecialChar

let b:current_syntax = 'halis'
