" Halis — Neovim / Vim plugin
" Stage 14 release of the Halis toolchain ships an official Neovim
" plugin that wires the hls-lsp language server, the hlfmt formatter,
" and the hllint linter into Vim/Neovim.
"
" Files:
"   editors/neovim/halis.vim   — runtime plugin (auto-loaded by Vim)
"   editors/neovim/ftdetect/halis.vim — file-type detection
"   editors/neovim/ftplugin/halis.vim  — file-type settings (format-on-save,
"                                         lint-on-save, keybindings)
"   editors/neovim/syntax/halis.vim    — syntax highlighting
"
" Install: copy or symlink `editors/neovim/` contents into your
" `~/.config/nvim/` (or `~/.vim/`). The plugin auto-discovers the
" Halis toolchain relative to the open buffer (looks for
" `<repo>/tools/hls-lsp.py` by walking up from the buffer's dir).

if exists('g:loaded_halis')
  finish
endif
let g:loaded_halis = 1

" Configuration variables (override in your vimrc).
let g:halis_python = get(g:, 'halis_python', 'python3')
let g:halis_lsp_path = get(g:, 'halis_lsp_path', '')
let g:halis_fmt_path = get(g:, 'halis_fmt_path', '')
let g:halis_lint_path = get(g:, 'halis_lint_path', '')
let g:halis_format_on_save = get(g:, 'halis_format_on_save', 0)
let g:halis_lint_on_save = get(g:, 'halis_lint_on_save', 1)

" Walk up from a buffer's directory looking for `tools/hls-lsp.py`.
function! halis#discover_tool(tool_name) abort
  " Explicit override wins.
  let l:override_var = 'g:halis_' .
    \ (a:tool_name ==# 'hls-lsp.py' ? 'lsp_path' :
    \  a:tool_name ==# 'hlfmt.py'   ? 'fmt_path' :
    \  a:tool_name ==# 'hllint.py'  ? 'lint_path' : '')
  if exists(l:override_var) && !empty({l:override_var})
    return {l:override_var}
  endif
  " Walk up to 6 levels.
  let l:dir = expand('%:p:h')
  for _ in range(6)
    let l:cand = l:dir . '/tools/' . a:tool_name
    if filereadable(l:cand)
      return l:cand
    endif
    let l:parent = fnamemodify(l:dir, ':h')
    if l:parent ==# l:dir
      break
    endif
    let l:dir = l:parent
  endfor
  " Fall back to the bare name (assume it's on PATH).
  return a:tool_name
endfunction

" Run hlfmt on the current buffer's file (in-place).
function! halis#format() abort
  let l:fmt = halis#discover_tool('hlfmt.py')
  let l:cmd = g:halis_python . ' ' . shellescape(l:fmt) . ' -w ' . shellescape(expand('%:p'))
  let l:out = system(l:cmd)
  if v:shell_error != 0
    echoerr 'hlfmt failed: ' . l:out
    return
  endif
  " Reload the file to pick up the formatted source.
  edit!
  echo 'Halis: formatted'
endfunction

" Run hllint on the current buffer's file (write warnings to the
" location list).
function! halis#lint() abort
  let l:lint = halis#discover_tool('hllint.py')
  let l:cmd = g:halis_python . ' ' . shellescape(l:lint) . ' --strict ' . shellescape(expand('%:p'))
  let l:out = system(l:cmd)
  " Parse output lines like:
  "   path:line: severity [L00X] message
  let l:errors = []
  for l:line in split(l:out, '\n')
    let l:m = matchlist(l:line, '^\(.\{-}\):\(\d\+\): \(\w\+\) \[\(L\d\+\)\] \(.*\)$')
    if !empty(l:m)
      call add(l:errors, {
        \ 'filename': l:m[1],
        \ 'lnum': l:m[2],
        \ 'type': l:m[3] ==# 'error' ? 'E' : 'W',
        \ 'text': '[' . l:m[4] . '] ' . l:m[5]
        \ })
    endif
  endfor
  call setloclist(0, l:errors, 'r')
  if !empty(l:errors)
    lwindow
  else
    lclose
    echo 'Halis: lint clean'
  endif
endfunction

" Start the LSP server (Neovim's built-in LSP, or vim-lsp).
function! halis#start_lsp() abort
  let l:lsp = halis#discover_tool('hls-lsp.py')
  if !has('nvim')
    echoerr 'Halis LSP requires Neovim (or vim-lsp for Vim8).'
    return
  endif
  lua << EOF
  local lsp = vim.lsp
  local cmd_path = vim.fn.escape(vim.g.halis_python, '"') .. ' ' .. vim.fn.expand('<SID>'):gsub('/$', '')
  -- Re-read the path discovered by Vim.
  local fmt_path = vim.api.nvim_get_var('halis_python')
  local args = { fmt_path, vim.fn.halis#discover_tool('hls-lsp.py') }
  local config = {
    name = 'hls-lsp',
    cmd = args,
    root_dir = vim.fs.dirname(vim.fn.expand('%:p')),
    filetypes = { 'halis' },
  }
  lsp.start_client(config)
EOF
endfunction

command! HalisFormat call halis#format()
command! HalisLint call halis#lint()
command! HalisRestartLSP call halis#start_lsp()
