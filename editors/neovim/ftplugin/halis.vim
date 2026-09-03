" Halis file-type plugin — runs on every .hls buffer.
" Stage 14 release.

" Indentation: 4 spaces (matches hlfmt's convention).
setlocal expandtab shiftwidth=4 tabstop=4 softtabstop=4

" Comments use `#`.
setlocal commentstring=#\ %s

" Format-on-save (opt-in).
if g:halis_format_on_save
  autocmd BufWritePre <buffer> call halis#format()
endif

" Lint-on-save (default on).
if g:halis_lint_on_save
  autocmd BufWritePost <buffer> call halis#lint()
endif

" Keybindings (local to the buffer).
nnoremap <buffer> <LocalLeader>f :HalisFormat<CR>
nnoremap <buffer> <LocalLeader>l :HalisLint<CR>
nnoremap <buffer> <LocalLeader>r :HalisRestartLSP<CR>

" Auto-start the language server when a .hls file is opened.
autocmd BufRead <buffer> call halis#start_lsp()
