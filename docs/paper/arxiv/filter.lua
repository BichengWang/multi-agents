--[[
Pandoc Lua filter for the arXiv version of docs/paper/index.md. build.py runs it (see its
docstring for usage); it needs the metadata build.py passes with -M (arxiv-reader, arxiv-repo,
arxiv-pin, arxiv-code-pin, arxiv-src-dir).

  1. The H1, the author line and "## Abstract" become template metadata; a "[PDF](...)" link
     in the author line is dropped.
  2. The "!!! note" admonition (a paragraph plus an indented code block) becomes a readerbox
     whose Markdown is parsed like the rest of the page.
  3. Citation markers "[n]" and "[n, m, ...]" become \cite{refn,...}; inline code becomes
     \texttt with break points; relative links are pinned to a commit on GitHub.
  4. Headings lose their manual numbers, each asserted equal to the number LaTeX prints;
     the appendices follow \appendix and their letters are asserted.
  5. <figure> blocks, "**Table X.**" captions with the table after them, fenced code blocks
     and the reference list become raw LaTeX.
Any construct it does not expect is an error, so the PDF cannot silently drop content.
With ARXIV_DEBUG=1 in the environment it prints each table's size, height and column widths.
]]

local stringify = pandoc.utils.stringify
local List = pandoc.List

local function fail(msg)
  error('filter.lua: ' .. msg, 0)
end

local CFG = {}
local WOPTS = pandoc.WriterOptions({ wrap_text = 'wrap-none' })

local function raw(text)
  return pandoc.RawBlock('latex', text)
end

local function inlines_latex(inls)
  local s = pandoc.write(pandoc.Pandoc({ pandoc.Plain(inls) }), 'latex', WOPTS)
  return (s:gsub('%s+$', ''))
end

local function blocks_latex(blocks)
  local s = pandoc.write(pandoc.Pandoc(blocks), 'latex', WOPTS)
  return (s:gsub('%s+$', ''))
end

local function chars_of(text)
  local out = {}
  for _, cp in utf8.codes(text) do
    out[#out + 1] = utf8.char(cp)
  end
  return out
end

---------------------------------------------------------------------------------------------
-- Inline code: \texttt with break points after separators and inside long hex strings.

local TT_ESCAPE = {
  ['\\'] = '\\textbackslash{}', ['{'] = '\\{', ['}'] = '\\}', ['$'] = '\\$', ['&'] = '\\&',
  ['#'] = '\\#', ['%'] = '\\%', ['_'] = '\\_', ['^'] = '\\textasciicircum{}',
  ['~'] = '\\textasciitilde{}', ['<'] = '\\textless{}', ['>'] = '\\textgreater{}',
  ["'"] = '\\textquotesingle{}', ['`'] = '\\textasciigrave{}', ['"'] = '\\textquotedbl{}',
}
-- break after these: freely, or (costlier) inside a name
local BREAK_AFTER = {
  ['/'] = '\\codebrk{}', ['.'] = '\\codebrk{}', [':'] = '\\codebrk{}', [','] = '\\codebrk{}',
  ['='] = '\\codebrk{}', ['+'] = '\\codebrk{}', ['@'] = '\\codebrk{}', [';'] = '\\codebrk{}',
  [')'] = '\\codebrk{}', ['>'] = '\\codebrk{}', ['('] = '\\codebrk{}',
  ['_'] = '\\codebrkx{}', ['-'] = '\\codebrkx{}',
}
local CODE_INFO = {} -- LaTeX of a code span -> { chars = n, longest = longest unbreakable run }
local TEXT_OF = {} -- LaTeX of a citation or of a path's link text -> the text it prints
-- a name shorter than this ("problem_statement") does not break after "_"
local NAME_BREAK_CHARS = 20

local function is_alnum(c)
  return c ~= nil and c:match('^%w$') ~= nil
end

-- For each character of cs that passes is_in, the length of its run and its place in it.
local function runs(cs, is_in)
  local len, pos = {}, {}
  local i, n = 1, #cs
  while i <= n do
    if is_in(cs[i]) then
      local j = i
      while j < n and is_in(cs[j + 1]) do
        j = j + 1
      end
      for k = i, j do
        len[k], pos[k] = j - i + 1, k - i + 1
      end
      i = j + 1
    else
      i = i + 1
    end
  end
  return len, pos
end

local function code_latex(text)
  local cs = chars_of(text)
  local n = #cs
  local run_len, run_pos = runs(cs, is_alnum)
  local name_len = runs(cs, function(c)
    return is_alnum(c) or c == '_'
  end)
  -- whether each character belongs to a space-separated token that starts with "--"
  local in_flag, flag = {}, false
  for k, c in ipairs(cs) do
    if c == ' ' then
      flag = false
    elseif k == 1 or cs[k - 1] == ' ' then
      flag = c == '-' and cs[k + 1] == '-'
    end
    in_flag[k] = flag
  end
  local out, seg, longest = {}, 0, 0
  for k, c in ipairs(cs) do
    if c == ' ' then
      out[#out + 1] = '\\codesp{}'
      longest, seg = math.max(longest, seg), 0
    else
      out[#out + 1] = TT_ESCAPE[c] or c
      seg = seg + 1
      local nxt, prev = cs[k + 1], cs[k - 1]
      if n >= 12 and k >= 3 and k <= n - 3 and nxt ~= nil and nxt ~= ' ' then
        local sep = BREAK_AFTER[c] and (is_alnum(nxt) or nxt == '(' or nxt == '<')
          and (c ~= '-' or is_alnum(prev))
          and (c ~= '_' or name_len[k] >= NAME_BREAK_CHARS)
          and (c ~= '.' or nxt:match('%a') ~= nil)
        if sep then
          -- a command-line flag breaks at the space before it; after one of its own "-" only
          -- when nothing else fits the line
          out[#out + 1] = (c == '-' and in_flag[k]) and '\\codebrkf{}' or BREAK_AFTER[c]
          longest, seg = math.max(longest, seg), 0
        elseif run_len[k] and run_len[k] >= 20 and run_pos[k] % 8 == 0 and run_pos[k] < run_len[k] then
          out[#out + 1] = '\\codebrkh{}'
          longest, seg = math.max(longest, seg), 0
        end
      end
    end
  end
  local latex = '\\texttt{' .. table.concat(out) .. '}'
  CODE_INFO[latex] = { chars = n, longest = math.max(longest, seg) }
  return pandoc.RawInline('latex', latex)
end

---------------------------------------------------------------------------------------------
-- Citation markers. A marker is "[n]" or "[n, m, ...]" in prose, possibly spread over
-- several Str elements ("[10," "11," "12]."), with punctuation before or after it.

local function cite_inlines(inls)
  local out = List()
  local i, n = 1, #inls
  while i <= n do
    local el = inls[i]
    local done = false
    if el.t == 'Str' and el.text:find('%[%d') then
      local s, j = el.text, i
      while not s:find('%]') and j + 2 <= n and inls[j + 1].t == 'Space' and inls[j + 2].t == 'Str' do
        s = s .. ' ' .. inls[j + 2].text
        j = j + 2
      end
      local pre, nums, post = s:match('^(.-)%[([%d, ]+)%](.*)$')
      local keys = List()
      if nums then
        for num in (nums .. ', '):gmatch('(.-), ') do
          if not num:match('^%d+$') then
            fail('malformed citation marker [' .. nums .. ']')
          end
          keys:insert('ref' .. num)
        end
        if table.concat(keys, ', '):gsub('ref', '') ~= nums then
          fail('malformed citation marker [' .. nums .. ']')
        end
      end
      if nums then
        if pre:find('%[') or post:find('%[%d') then
          fail('two citation markers in one word: ' .. s)
        end
        if pre ~= '' then
          out:insert(pandoc.Str(pre))
        end
        local latex = '\\cite{' .. table.concat(keys, ',') .. '}'
        TEXT_OF[latex] = '[' .. nums .. ']'
        out:insert(pandoc.RawInline('latex', latex))
        if post ~= '' then
          out:insert(pandoc.Str(post))
        end
        i, done = j + 1, true
      end
    end
    if not done then
      out:insert(el)
      i = i + 1
    end
  end
  return out
end

---------------------------------------------------------------------------------------------
-- Links: a relative link to a repository file becomes a GitHub URL pinned to a commit.

local function normalize_path(path)
  local parts = List()
  for part in path:gmatch('[^/]+') do
    if part == '..' then
      if #parts == 0 then
        fail('link leaves the repository: ' .. path)
      end
      parts:remove()
    elseif part ~= '.' then
      parts:insert(part)
    end
  end
  return table.concat(parts, '/')
end

-- Link text that is a path ("results/2026-10-haiku-pilot/") breaks only after a "/", so that
-- a line never ends at one of its hyphens.
local function path_text(link)
  if #link.content ~= 1 or link.content[1].t ~= 'Str' then
    return
  end
  local text = link.content[1].text
  if not (text:find('/', 1, true) and text:match('^[%w%._/-]+$')) then
    return
  end
  local parts = List()
  for part in text:gmatch('[^/]*/?') do
    if part ~= '' then
      parts:insert('\\mbox{' .. part:gsub('_', '\\_') .. '}')
    end
  end
  local latex = table.concat(parts, '\\hspace{0pt}')
  TEXT_OF[latex] = text
  link.content = { pandoc.RawInline('latex', latex) }
end

-- Files of the paper's directory are pinned to the commit that adds the paper, every other
-- file to the commit the paper cites code and data at.
local function pin_link(link)
  local target = link.target
  if target:match('^%a[%w+.-]*:') or target:match('^#') or target:match('^/') then
    return nil
  end
  local path, anchor = target:match('^([^#]*)(.*)$')
  if path:match('%.md$') then
    fail('relative link to another Markdown page: ' .. target)
  end
  local repo_path = normalize_path(CFG.src_dir .. '/' .. path)
  local is_dir = path:sub(-1) == '/' or pcall(pandoc.system.list_directory, repo_path)
  local kind = is_dir and 'tree' or 'blob'
  local pin = repo_path:sub(1, #CFG.src_dir + 1) == CFG.src_dir .. '/' and CFG.pin or CFG.code_pin
  link.target = CFG.repo .. '/' .. kind .. '/' .. pin .. '/' .. repo_path .. anchor
  path_text(link)
  return link
end

---------------------------------------------------------------------------------------------
-- Measuring content, to give table columns widths that fit the text width.

local function char_em(c)
  if c:match('^%u$') then
    return 0.70
  elseif c:match('^%l$') then
    return 0.47
  elseif c:match('^%d$') then
    return 0.50
  elseif c:match('^[%.,;:\'!|]$') then
    return 0.28
  end
  return 0.50
end

-- Returns { total = natural width in em, longest = widest unbreakable piece in em }.
local function measure(inls, bold)
  local m = { total = 0, longest = 0 }
  local cur = 0
  local k = bold and 1.07 or 1.0
  local function text(s, per)
    for _, c in ipairs(chars_of(s)) do
      local w = (per or char_em(c)) * k
      m.total, cur = m.total + w, cur + w
    end
  end
  local function gap()
    m.total = m.total + 0.33
    m.longest, cur = math.max(m.longest, cur), 0
  end
  local function walk(list)
    for _, el in ipairs(list) do
      if el.t == 'Str' then
        text(el.text)
      elseif el.t == 'Space' or el.t == 'SoftBreak' or el.t == 'LineBreak' then
        gap()
      elseif el.t == 'RawInline' and CODE_INFO[el.text] then
        local info = CODE_INFO[el.text]
        m.total = m.total + info.chars * 0.525
        m.longest = math.max(m.longest, info.longest * 0.525, cur)
        cur = 0
      elseif el.t == 'RawInline' and TEXT_OF[el.text] then
        text(TEXT_OF[el.text])
      elseif el.t == 'Quoted' then
        text('""')
        walk(el.content)
      elseif el.content then
        walk(el.content)
      else
        fail('cannot measure inline ' .. el.t)
      end
    end
  end
  walk(inls)
  m.longest = math.max(m.longest, cur)
  return m
end

local function cell_inlines(cell)
  local blocks = cell.contents
  if #blocks == 0 then
    return List()
  end
  if #blocks ~= 1 or (blocks[1].t ~= 'Plain' and blocks[1].t ~= 'Para') then
    fail('table cell with more than one paragraph')
  end
  return blocks[1].content
end

---------------------------------------------------------------------------------------------
-- Tables. Geometry of the page, in pt: \linewidth of 6.5in, font sizes of the 11pt class.

local LINEWIDTH = 469.75
local TEXTHEIGHT = 650.43
local SIZES = {
  small = { pt = 10, baseline = 12 },
  footnotesize = { pt = 9, baseline = 11 },
}
local CAPTION = SIZES.small
-- Tables taller than this (estimated) may break across pages; shorter ones never do.
local BREAKABLE_ABOVE = 0.34 * TEXTHEIGHT
-- a table that fits the line gets its natural column widths times this, at most the line
local NATURAL_SLACK = 1.1

-- Per-table settings, by label: size, tabcolsep (pt), breakable, column weights that replace
-- the computed widths (so that a value or a task id stays on one line), and a separator after
-- which every body cell starts a new line (so that a cell's values line up across a row).
local TUNE = {
  ['1'] = { weights = { 34, 22, 22, 22 }, sep = 5, breakable = false },
  ['3'] = { weights = { 11, 52, 15, 22 } },
  ['4'] = { weights = { 17, 30, 34, 19 } },
  B1 = { newline = '; ', breakable = false },
  C2 = { weights = { 37.5, 6.5, 56 } },
  F2 = { weights = { 21, 12, 16, 51 } },
  G1 = { weights = { 12, 19, 17, 24, 28 } },
  H1 = { weights = { 15, 13, 55, 17 } },
  I1 = { weights = { 27, 32, 41 } },
}

local function plan_table(caption_para, tbl, in_appendix, after_heading)
  local label = stringify(caption_para.content[1]):match('^Table (%u?%d+)%.$')
  local tune = TUNE[label] or {}
  local size = tune.size or (in_appendix and 'footnotesize' or 'small')
  local font = SIZES[size]
  local sep = tune.sep or 6
  if #tbl.bodies ~= 1 or #tbl.foot.rows ~= 0 or #tbl.head.rows ~= 1 then
    fail('Table ' .. label .. ': expected one header row and one body')
  end
  local head = tbl.head.rows[1]
  local rows = tbl.bodies[1].body
  local ncol = #tbl.colspecs
  local function row_cells(row)
    local cells = List()
    for _, cell in ipairs(row.cells) do
      if cell.row_span ~= 1 or cell.col_span ~= 1 then
        fail('Table ' .. label .. ': spanning cells are not supported')
      end
      cells:insert(cell_inlines(cell))
    end
    if #cells ~= ncol then
      fail('Table ' .. label .. ': a row has ' .. #cells .. ' cells, not ' .. ncol)
    end
    return cells
  end
  local head_cells = row_cells(head)
  local body = List()
  for _, row in ipairs(rows) do
    body:insert(row_cells(row))
  end

  -- natural and minimum width of every column, in em of the table font
  local maxw, minw = {}, {}
  for c = 1, ncol do
    maxw[c], minw[c] = 0.5, 0.5
  end
  local function take(cells, bold)
    for c, inls in ipairs(cells) do
      local m = measure(inls, bold)
      maxw[c] = math.max(maxw[c], m.total * 1.04 + 0.3)
      minw[c] = math.max(minw[c], m.longest * 1.04 + 0.3)
    end
  end
  take(head_cells, true)
  for _, cells in ipairs(body) do
    take(cells, false)
  end
  local avail = (LINEWIDTH - 2 * (ncol - 1) * sep) / font.pt
  local smax, smin = 0, 0
  for c = 1, ncol do
    smax, smin = smax + maxw[c], smin + minw[c]
  end
  local width = {}
  -- a table whose content fits the line with room to spare keeps (nearly) its natural width
  -- and is centred, rather than spreading its columns over the line
  local full = true
  if tune.weights then
    local s = 0
    for c = 1, ncol do
      s = s + tune.weights[c]
    end
    for c = 1, ncol do
      width[c] = avail * tune.weights[c] / s
    end
  elseif smax <= avail then
    local scale = math.min(avail / smax, NATURAL_SLACK)
    full = scale == avail / smax
    for c = 1, ncol do
      width[c] = maxw[c] * scale
    end
  elseif smin >= avail then
    for c = 1, ncol do
      width[c] = avail * minw[c] / smin
    end
  else
    for c = 1, ncol do
      width[c] = minw[c] + (maxw[c] - minw[c]) * (avail - smin) / (smax - smin)
    end
  end
  for c = 1, ncol do
    if width[c] + 0.05 < minw[c] then
      io.stderr:write(string.format('filter.lua: Table %s column %d is narrower than its longest word\n', label, c))
    end
  end
  -- fractions in units of 1/10000 of the space left for the columns, summing to 10000 for a
  -- table as wide as the line
  local frac, acc = {}, 0
  for c = 1, ncol do
    frac[c] = math.floor(width[c] / avail * 10000 + 0.5)
    acc = acc + frac[c]
  end
  if full then
    frac[ncol] = frac[ncol] + 10000 - acc
  end
  -- a column of plain numbers ("157", "+9.9", "0.22") is set flush right
  local numeric = {}
  for c = 1, ncol do
    numeric[c] = #body > 0
    for _, cells in ipairs(body) do
      local s = stringify(cells[c]):gsub('^−', ''):gsub('^%+', '')
      if not s:match('^%d[%d.,]*$') then
        numeric[c] = false
      end
    end
  end

  -- height estimate, in pt
  local function lines(cells, bold)
    local l = 1
    for c, inls in ipairs(cells) do
      local m = measure(inls, bold)
      l = math.max(l, math.ceil((m.total * 1.04 + 0.3) / width[c] - 0.02))
      if tune.newline and not bold then
        local _, breaks = stringify(inls):gsub(tune.newline, '')
        l = math.max(l, breaks + 1)
      end
    end
    return l
  end
  local row_lines = List()
  local total_lines = 0
  for _, cells in ipairs(body) do
    local l = lines(cells, false)
    row_lines:insert(l)
    total_lines = total_lines + l
  end
  local spaced = total_lines > #body
  local head_lines = lines(head_cells, true)
  local cap = measure(caption_para.content, false)
  local cap_lines = math.ceil(cap.total * CAPTION.pt / LINEWIDTH * 1.03)
  local row_gap = spaced and 2 or 0
  local caption_h = cap_lines * CAPTION.baseline + 6
  local head_h = head_lines * font.baseline + 8
  local height = caption_h + head_h + total_lines * font.baseline + #body * row_gap + 6
  local keep = caption_h + head_h + (row_lines[1] + (row_lines[2] or 0)) * font.baseline + 2 * row_gap
  local breakable = tune.breakable
  if breakable == nil then
    -- a long table right after a heading floats when it fits a page: longtable may break
    -- between the heading and the table, and \\Needspace cannot measure a page that so far
    -- holds only floats
    breakable = height > BREAKABLE_ABOVE and not (after_heading and height < 0.9 * TEXTHEIGHT)
  end
  if os.getenv('ARXIV_DEBUG') then
    local w = {}
    for c = 1, ncol do
      w[c] = string.format('%.1f', frac[c] / 100)
    end
    io.stderr:write(string.format('Table %-3s %-12s height %5.0fpt keep %4.0fpt %s %s widths %% %s\n', label, size,
      height, keep, breakable and 'breakable' or 'whole    ', full and 'full   ' or 'natural', table.concat(w, ' ')))
  end
  return {
    label = label, size = size, sep = sep, ncol = ncol, frac = frac, caption = caption_para.content,
    head = head_cells, body = body, spaced = spaced, height = height, keep = keep,
    breakable = breakable, full = full, numeric = numeric, newline = tune.newline,
  }
end

-- in a table cell, a range ("−2.7 to +10.9") and "post hoc" stay on one line
local function tie_cell(s)
  s = s:gsub('(%d) to (%S)', '%1~to~%2')
  s = s:gsub('post hoc', 'post~hoc')
  return s
end

local function render_table(p)
  local spec = List()
  for c = 1, p.ncol do
    spec:insert(string.format('%s{\\dimexpr(\\linewidth-%d\\tabcolsep)*%d/10000\\relax}',
      p.numeric[c] and 'R' or 'P', 2 * (p.ncol - 1), p.frac[c]))
  end
  local colspec = '@{}' .. table.concat(spec, '') .. '@{}'
  local head = List()
  for _, inls in ipairs(p.head) do
    local s = inlines_latex(inls)
    head:insert(s ~= '' and ('\\textbf{' .. s .. '}') or '')
  end
  local head_row = table.concat(head, ' & ') .. ' \\\\'
  local caption = '\\caption*{' .. inlines_latex(p.caption) .. '}'
  local gap = p.spaced and '\\addlinespace[2pt]' or ''
  local rows = List()
  for r, cells in ipairs(p.body) do
    local parts = List()
    for _, inls in ipairs(cells) do
      local s = tie_cell(inlines_latex(inls))
      if p.newline then
        s = s:gsub(p.newline, (p.newline:gsub('%s+$', '')) .. '\\newline ')
      end
      parts:insert(s)
    end
    local ending = ' \\\\'
    if p.breakable and (r == 1 or r == #p.body - 1) then
      ending = ' \\\\*'
    end
    rows:insert(table.concat(parts, ' & ') .. ending .. (r < #p.body and gap or ''))
  end
  local out = List()
  local font = '\\' .. p.size .. string.format('\\setlength{\\tabcolsep}{%dpt}', p.sep)
  out:insert('% Table ' .. p.label)
  if p.breakable then
    -- in the text flow, so it may break across pages; caption and header stay with row 1
    out:insert('\\begingroup' .. font)
    out:insert('\\begin{longtable}{' .. colspec .. '}')
    out:insert(caption .. '\\\\')
    out:insert('\\toprule')
    out:insert(head_row)
    out:insert('\\midrule')
    out:insert('\\endfirsthead')
    -- a continuation page names the table above its repeated header
    out:insert(string.format('\\multicolumn{%d}{@{}l}{\\small\\textbf{Table %s} (continued)}\\\\[3pt]', p.ncol, p.label))
    out:insert('\\toprule')
    out:insert(head_row)
    out:insert('\\midrule')
    out:insert('\\endhead')
    out:insert('\\bottomrule')
    out:insert('\\endfoot')
    out:extend(rows)
    out:insert('\\end{longtable}')
    out:insert('\\endgroup')
  else
    -- a float, so it never breaks and leaves no gap in the text
    out:insert('\\begin{table}[htbp]')
    out:insert(font .. (p.full and '' or '\\centering'))
    out:insert(caption)
    out:insert('\\begin{tabular}{' .. colspec .. '}')
    out:insert('\\toprule')
    out:insert(head_row)
    out:insert('\\midrule')
    out:extend(rows)
    out:insert('\\bottomrule')
    out:insert('\\end{tabular}')
    out:insert('\\end{table}')
  end
  return raw(table.concat(out, '\n'))
end

local function table_caption_label(block)
  if block and block.t == 'Para' and block.content[1] and block.content[1].t == 'Strong' then
    return stringify(block.content[1]):match('^Table (%u?%d+)%.$')
  end
  return nil
end

---------------------------------------------------------------------------------------------
-- Figures: <figure><img src="figures/X.svg" ...><figcaption>...</figcaption></figure>.

local function html_strong(inls)
  local out, stack = List(), nil
  for _, el in ipairs(inls) do
    if el.t == 'RawInline' and el.format == 'html' then
      if el.text == '<strong>' and stack == nil then
        stack = List()
      elseif el.text == '</strong>' and stack ~= nil then
        out:insert(pandoc.Strong(stack))
        stack = nil
      else
        fail('unexpected HTML in a figure caption: ' .. el.text)
      end
    elseif stack ~= nil then
      stack:insert(el)
    else
      out:insert(el)
    end
  end
  if stack ~= nil then
    fail('unclosed <strong> in a figure caption')
  end
  return out
end

local function figure_latex(blocks, i)
  local src, caption, in_caption = nil, nil, false
  local j = i + 1
  while true do
    local b = blocks[j]
    if b == nil then
      fail('unclosed <figure>')
    end
    if b.t == 'RawBlock' and b.format == 'html' then
      if b.text == '</figure>' then
        break
      elseif b.text == '<figcaption>' then
        in_caption = true
      elseif b.text == '</figcaption>' then
        in_caption = false
      else
        fail('unexpected HTML in a figure: ' .. b.text)
      end
    elseif (b.t == 'Plain' or b.t == 'Para') and in_caption then
      if caption then
        fail('figure with two caption paragraphs')
      end
      caption = html_strong(b.content)
    elseif (b.t == 'Plain' or b.t == 'Para') and #b.content == 1 and b.content[1].t == 'RawInline' then
      src = b.content[1].text:match('^<img src="([^"]+)"')
      if not src then
        fail('unexpected figure content: ' .. b.content[1].text)
      end
    else
      fail('unexpected block in a figure: ' .. b.t)
    end
    j = j + 1
  end
  if not src or not caption then
    fail('figure without an image or a caption')
  end
  local name = src:match('^figures/([%w%-]+)%.svg$')
  if not name then
    fail('figure source is not figures/<name>.svg: ' .. src)
  end
  if not stringify(caption):match('^Figure %u?%d+%. ') then
    fail('figure caption does not start with its label: ' .. stringify(caption))
  end
  local latex = table.concat({
    '\\begin{figure}[htbp]',
    '\\centering',
    '\\includegraphics[width=\\linewidth]{figures/' .. name .. '.pdf}',
    '\\caption*{' .. inlines_latex(caption) .. '}',
    '\\end{figure}',
  }, '\n')
  return raw(latex), j
end

---------------------------------------------------------------------------------------------
-- Fenced code: fvextra Verbatim, content byte for byte. Lines longer than a scriptsize line
-- (109 characters in the frame) break at spaces; a long token with no space breaks after "+"
-- or "," where it has them.

local CODE_LINE_CHARS = 109

local function code_block_latex(b)
  if b.text:find('\\end{Verbatim}', 1, true) then
    fail('code block contains \\end{Verbatim}')
  end
  local options = {}
  local after, anywhere = {}, false
  for line in (b.text .. '\n'):gmatch('([^\n]*)\n') do
    for token in line:gmatch('%S+') do
      if utf8.len(token) > CODE_LINE_CHARS then
        local found = false
        for _, c in ipairs({ '+', ',' }) do
          if token:find(c, 1, true) then
            after[c], found = true, true
          end
        end
        anywhere = anywhere or not found
      end
    end
  end
  if anywhere then
    options[#options + 1] = 'breakanywhere=true'
  end
  local chars = {}
  for _, c in ipairs({ '+', ',' }) do
    if after[c] then
      chars[#chars + 1] = c
    end
  end
  if #chars > 0 then
    options[#options + 1] = 'breakafter={' .. table.concat(chars) .. '}'
  end
  local opt = #options > 0 and ('[' .. table.concat(options, ',') .. ']') or ''
  return raw('\\begin{Verbatim}' .. opt .. '\n' .. b.text .. '\n\\end{Verbatim}')
end

local function code_height_lines(b)
  local n = 0
  for line in (b.text .. '\n'):gmatch('([^\n]*)\n') do
    n = n + math.max(1, math.ceil(utf8.len(line) / CODE_LINE_CHARS))
  end
  -- scriptsize lines (9.5pt) in units of the 13.6pt body baseline, plus the frame
  return n * 9.5 / 13.6 + 1.5
end

---------------------------------------------------------------------------------------------
-- Pass 1: title, author line, abstract, admonition.

local function front_matter(doc)
  local blocks = doc.blocks
  local h1 = blocks[1]
  if not (h1 and h1.t == 'Header' and h1.level == 1) then
    fail('the page must start with its H1 title')
  end
  local byline = blocks[2]
  if not (byline and byline.t == 'Para') then
    fail('the author line must follow the title')
  end
  local fields, cur = List(), List()
  for _, el in ipairs(byline.content) do
    if el.t == 'Str' and el.text == '·' then
      fields:insert(cur)
      cur = List()
    elseif not (el.t == 'Space' and (#cur == 0)) then
      cur:insert(el)
    end
  end
  fields:insert(cur)
  for _, f in ipairs(fields) do
    while #f > 0 and f[#f].t == 'Space' do
      f:remove()
    end
  end
  if #fields ~= 3 and #fields ~= 4 then
    fail('author line must be "name · repository link · date [· PDF link]"')
  end
  local repo = fields[2]
  if not (#repo == 1 and repo[1].t == 'Link' and repo[1].target == CFG.repo) then
    fail('the second field of the author line must link to ' .. CFG.repo)
  end
  if not stringify(fields[3]):match('^%a+ %d%d%d%d$') then
    fail('the third field of the author line must be a date such as "November 2026"')
  end
  if #fields == 4 then
    local pdf = fields[4]
    if not (#pdf == 1 and pdf[1].t == 'Link' and pdf[1].target == 'paper.pdf') then
      fail('the fourth field of the author line may only be the link [PDF](paper.pdf)')
    end
  end
  doc.meta.title = pandoc.MetaInlines(h1.content)
  -- the typeset title starts a new line after its first ": ", so the subtitle is not split;
  -- the PDF's title metadata keeps the plain title
  local title_tex, broken = List(), false
  for k, el in ipairs(h1.content) do
    local prev = h1.content[k - 1]
    if not broken and el.t == 'Space' and prev and prev.t == 'Str' and prev.text:match(':$') then
      title_tex:insert(pandoc.RawInline('latex', '\\\\ '))
      broken = true
    else
      title_tex:insert(el)
    end
  end
  doc.meta['title-tex'] = pandoc.MetaInlines(title_tex)
  doc.meta.author = pandoc.MetaInlines(fields[1])
  doc.meta.date = pandoc.MetaInlines(fields[3])
  doc.meta['repo-url'] = pandoc.MetaString(CFG.repo)
  doc.meta.converter = pandoc.MetaString(tostring(PANDOC_VERSION))

  local h2 = blocks[3]
  if not (h2 and h2.t == 'Header' and h2.level == 2 and stringify(h2.content) == 'Abstract') then
    fail('"## Abstract" must follow the author line')
  end
  local abstract, k = List(), 4
  while blocks[k] and blocks[k].t ~= 'Header' do
    abstract:insert(blocks[k])
    k = k + 1
  end
  doc.meta.abstract = pandoc.MetaBlocks(abstract)

  local rest = List()
  while k <= #blocks do
    local b = blocks[k]
    local nxt = blocks[k + 1]
    if b.t == 'Para' and b.content[1] and b.content[1].t == 'Str' and b.content[1].text == '!!!' then
      local title = nil
      for _, el in ipairs(b.content) do
        if el.t == 'Quoted' then
          title = el.content
        end
      end
      if not title or not nxt or nxt.t ~= 'CodeBlock' then
        fail('an admonition needs a quoted title and an indented body')
      end
      local inner = pandoc.read(nxt.text, CFG.reader).blocks
      -- the title travels as the Div's first block, so the inline passes reach it too
      inner:insert(1, pandoc.Plain(title))
      rest:insert(pandoc.Div(inner, pandoc.Attr('', { 'readerbox' })))
      k = k + 2
    else
      rest:insert(b)
      k = k + 1
    end
  end
  doc.blocks = rest
  return doc
end

---------------------------------------------------------------------------------------------
-- Pass 3: block structure.

local UNNUMBERED = { ['Data and code'] = true, ['Safety note'] = true, ['References'] = true }
local HEADING_KEEP = 3 -- body lines a heading keeps below it

local function needspace_pt(pt)
  return raw(string.format('\\Needspace{%dpt}', math.ceil(pt)))
end

-- In the reference list, a link whose text is not its URL ("tools-reference") is followed by
-- the URL, so that a printed copy can still be followed; a DOI link prints its DOI already.
local function print_urls(blocks)
  return pandoc.Div(blocks):walk({
    Inlines = function(inls)
      local out = List()
      for _, el in ipairs(inls) do
        out:insert(el)
        if el.t == 'Link' and stringify(el.content) ~= el.target and not el.target:match('^https://doi%.org/') then
          out:extend({ pandoc.Space(), pandoc.Str('('), pandoc.Link({ pandoc.Str(el.target) }, el.target),
            pandoc.Str(')') })
        end
      end
      return out
    end,
  }).content
end

local function drop(inls, n)
  local out = List()
  for k = n + 1, #inls do
    out:insert(inls[k])
  end
  return out
end

local function structure(doc)
  local blocks = doc.blocks
  local out = List()
  local sec, sub, app = 0, 0, 0
  local in_appendix = false
  local i = 1
  local function next_table_plan(k)
    if table_caption_label(blocks[k]) and blocks[k + 1] and blocks[k + 1].t == 'Table' then
      return plan_table(blocks[k], blocks[k + 1], in_appendix, true)
    end
    return nil
  end
  while i <= #blocks do
    local b = blocks[i]
    if b.t == 'Header' then
      local text = stringify(b.content)
      local title, command
      if b.level == 2 and text:match('^Appendix ') then
        local letter = text:match('^Appendix (%u)%. ')
        if not letter or b.content[3].text ~= letter .. '.' then
          fail('appendix heading must read "Appendix X. Title": ' .. text)
        end
        app = app + 1
        local expected = string.char(64 + app)
        if letter ~= expected then
          fail('appendix "' .. text .. '" is lettered ' .. letter .. ', LaTeX prints ' .. expected)
        end
        if not in_appendix then
          out:insert(raw('\\clearpage\n\\appendix'))
          in_appendix = true
        end
        title, command = drop(b.content, 4), '\\section'
      elseif b.level == 2 and text:match('^%d') then
        local num = text:match('^(%d+)%. ')
        if in_appendix then
          fail('numbered section after the appendices: ' .. text)
        end
        sec, sub = sec + 1, 0
        if num ~= tostring(sec) or b.content[1].text ~= num .. '.' then
          fail('section "' .. text .. '" is numbered ' .. tostring(num) .. ', LaTeX prints ' .. sec)
        end
        title, command = drop(b.content, 2), '\\section'
      elseif b.level == 3 then
        local num = text:match('^(%d+%.%d+) ')
        sub = sub + 1
        local expected = sec .. '.' .. sub
        if num ~= expected or b.content[1].text ~= num then
          fail('subsection "' .. text .. '" is numbered ' .. tostring(num) .. ', LaTeX prints ' .. expected)
        end
        title, command = drop(b.content, 2), '\\subsection'
      elseif b.level == 2 and UNNUMBERED[text] then
        title, command = b.content, '\\section*'
      else
        fail('unexpected heading: ' .. text)
      end
      if title[1] and title[1].t == 'Space' then
        fail('heading number not followed by exactly one space: ' .. text)
      end
      -- keep the heading with what follows: body lines, or the start of a long table
      local keep = HEADING_KEEP * 13.6
      local plan = next_table_plan(i + 1)
      if plan and plan.breakable then
        keep = plan.keep + 13.6
      end
      -- the main text's floats are placed before the back matter starts, and an appendix's
      -- before the next appendix (both before the space the heading needs is measured)
      if text == 'Data and code' or (in_appendix and command == '\\section' and app > 1) then
        out:insert(raw('\\FloatBarrier'))
      end
      out:insert(needspace_pt(keep + 40))
      if text == 'References' then
        local list = blocks[i + 1]
        if not (list and list.t == 'OrderedList' and list.start == 1) then
          fail('"## References" must be followed by a numbered list starting at 1')
        end
        local items = List()
        for n, item in ipairs(list.content) do
          items:insert('\\bibitem{ref' .. n .. '} ' .. blocks_latex(print_urls(item)))
        end
        out:insert(raw('\\phantomsection\\addcontentsline{toc}{section}{References}\n'
          .. '\\begingroup\\small\\raggedright\n\\begin{thebibliography}{99}\n'
          .. table.concat(items, '\n') .. '\n\\end{thebibliography}\n\\endgroup'))
        i = i + 2
      else
        local heading = command .. '{' .. inlines_latex(title) .. '}'
        if command == '\\section*' then
          heading = heading .. '\n\\phantomsection\\addcontentsline{toc}{section}{' .. inlines_latex(title) .. '}'
        end
        out:insert(raw(heading))
        i = i + 1
      end
    elseif table_caption_label(b) then
      local tbl = blocks[i + 1]
      if not (tbl and tbl.t == 'Table') then
        fail('caption of Table ' .. table_caption_label(b) .. ' is not followed by a table')
      end
      local plan = plan_table(b, tbl, in_appendix, blocks[i - 1] and blocks[i - 1].t == 'Header')
      if plan.breakable then
        out:insert(needspace_pt(plan.keep + 12))
      end
      out:insert(render_table(plan))
      i = i + 2
    elseif b.t == 'Table' then
      fail('table without a "**Table X.**" caption before it')
    elseif b.t == 'RawBlock' and b.format == 'html' and b.text == '<figure>' then
      local fig, last = figure_latex(blocks, i)
      out:insert(fig)
      i = last + 1
    elseif b.t == 'CodeBlock' then
      out:insert(code_block_latex(b))
      i = i + 1
    elseif b.t == 'Div' and b.classes:includes('readerbox') then
      out:insert(raw('\\begin{readerbox}{' .. inlines_latex(b.content[1].content) .. '}'))
      out:extend(drop(b.content, 1))
      out:insert(raw('\\end{readerbox}'))
      i = i + 1
    elseif (b.t == 'Para' or b.t == 'Plain') and blocks[i + 1] and blocks[i + 1].t == 'CodeBlock' then
      -- an introducing paragraph stays on the page of its code block
      local m = measure(b.content, false)
      local lines = math.ceil(m.total * 11 / LINEWIDTH * 1.03)
      out:insert(needspace_pt((lines + code_height_lines(blocks[i + 1]) + 1) * 13.6))
      out:insert(b)
      out:insert(raw('\\nopagebreak'))
      i = i + 1
    else
      out:insert(b)
      i = i + 1
    end
  end
  doc.blocks = out
  return doc
end

---------------------------------------------------------------------------------------------
-- Pass 4: nothing the converter does not handle may remain.

local function leftovers(doc)
  doc:walk({
    RawInline = function(el)
      if el.format ~= 'latex' then
        fail('raw ' .. el.format .. ' inline left: ' .. el.text)
      end
    end,
    RawBlock = function(el)
      if el.format ~= 'latex' then
        fail('raw ' .. el.format .. ' block left: ' .. el.text)
      end
    end,
    Header = function(el)
      fail('heading left: ' .. stringify(el))
    end,
    Table = function()
      fail('table left')
    end,
    CodeBlock = function()
      fail('code block left')
    end,
    Image = function()
      fail('image left')
    end,
    Note = function()
      fail('footnote left')
    end,
  })
end

function Pandoc(doc)
  for key, field in pairs({ reader = 'arxiv-reader', repo = 'arxiv-repo', pin = 'arxiv-pin',
    code_pin = 'arxiv-code-pin', src_dir = 'arxiv-src-dir' }) do
    if doc.meta[field] == nil then
      fail('missing metadata ' .. field .. ' (run docs/paper/arxiv/build.py)')
    end
    CFG[key] = stringify(doc.meta[field])
    doc.meta[field] = nil
  end
  doc = front_matter(doc)
  doc = doc:walk({
    Code = function(el)
      return code_latex(el.text)
    end,
    Link = pin_link,
  })
  doc = doc:walk({ Inlines = cite_inlines })
  doc = structure(doc)
  leftovers(doc)
  return doc
end
