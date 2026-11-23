import Foundation

// Ported from GiSTRA (GistraCore/ChatMarkdown.swift, the same author's code), unchanged apart from access levels.

/// Block-level Markdown for AI chat replies: paragraphs, headings, (nested) lists, code fences, quotes,
/// GitHub tables and rules. The parser is a single line-oriented pass so a reply streaming in ~10×/s can be
/// re-parsed on every change; unfinished fences and tables still come out as their block.
/// Inline text is kept as source and turned into an `AttributedString` with `inline(_:)`.
enum ChatMarkdown {
    enum Alignment: Sendable, Equatable { case leading, center, trailing }

    struct ListItem: Sendable, Equatable {
        /// Nesting depth, 0 for the outermost items.
        var level: Int
        /// The number written for an ordered item ("3."), nil for a bullet.
        var number: String?
        /// `- [ ]` / `- [x]` task items.
        var checked: Bool?
        var text: String
    }

    struct Table: Sendable, Equatable {
        var header: [String]
        var alignments: [Alignment]
        /// Every row has exactly `header.count` cells.
        var rows: [[String]]
    }

    indirect enum Block: Sendable, Equatable {
        case paragraph(String)
        case heading(level: Int, text: String)
        case list([ListItem])
        case code(language: String?, text: String)
        case quote([Block])
        case table(Table)
        case rule
    }

    // MARK: - Blocks

    static func parse(_ text: String) -> [Block] {
        let lines = text.replacingOccurrences(of: "\r\n", with: "\n").split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        return parse(lines: lines[...])
    }

    private static func parse(lines: ArraySlice<String>) -> [Block] {
        var blocks: [Block] = []
        var paragraph: [String] = []
        var i = lines.startIndex

        func flush() {
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: "\n"))) }
            paragraph = []
        }

        while i < lines.endIndex {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.isEmpty { flush(); i += 1; continue }

            // Code fence: runs to the closing fence, or to the end while it is still streaming.
            if let fence = fenceOpen(trimmed) {
                flush()
                var body: [String] = []
                i += 1
                while i < lines.endIndex {
                    let t = lines[i].trimmingCharacters(in: .whitespaces)
                    if t.hasPrefix(fence.marker), t.allSatisfy({ $0 == fence.marker.first }) { i += 1; break }
                    body.append(lines[i])
                    i += 1
                }
                blocks.append(.code(language: fence.language, text: body.joined(separator: "\n")))
                continue
            }

            if let heading = heading(trimmed) {
                flush()
                blocks.append(heading)
                i += 1
                continue
            }

            if isRule(trimmed) {
                flush()
                blocks.append(.rule)
                i += 1
                continue
            }

            if trimmed.hasPrefix(">") {
                flush()
                var inner: [String] = []
                while i < lines.endIndex {
                    let t = lines[i].trimmingCharacters(in: .whitespaces)
                    guard t.hasPrefix(">") else { break }
                    var rest = t.dropFirst()
                    if rest.first == " " { rest = rest.dropFirst() }
                    inner.append(String(rest))
                    i += 1
                }
                blocks.append(.quote(parse(lines: inner[...])))
                continue
            }

            // Table: a row with pipes followed by a `---|---` delimiter row.
            if trimmed.contains("|"), i + 1 < lines.endIndex, let alignments = delimiterRow(lines[i + 1]) {
                flush()
                let header = cells(trimmed)
                let count = max(header.count, alignments.count)
                func pad(_ row: [String]) -> [String] {
                    row.count >= count ? Array(row.prefix(count)) : row + Array(repeating: "", count: count - row.count)
                }
                var rows: [[String]] = []
                i += 2
                while i < lines.endIndex {
                    let t = lines[i].trimmingCharacters(in: .whitespaces)
                    guard !t.isEmpty, t.contains("|") else { break }
                    rows.append(pad(cells(t)))
                    i += 1
                }
                let aligned = alignments.count >= count ? Array(alignments.prefix(count))
                    : alignments + Array(repeating: .leading, count: count - alignments.count)
                blocks.append(.table(Table(header: pad(header), alignments: aligned, rows: rows)))
                continue
            }

            if listMarker(line) != nil {
                flush()
                var items: [ListItem] = []
                var indents: [Int] = []
                var blankBefore = false
                while i < lines.endIndex {
                    let current = lines[i]
                    let t = current.trimmingCharacters(in: .whitespaces)
                    if t.isEmpty { blankBefore = true; i += 1; continue }
                    if let marker = listMarker(current) {
                        // Switching between bullets and numbers at the outer level starts a new list.
                        if let outer = indents.first, marker.indent <= outer,
                           let top = items.last(where: { $0.level == 0 }), (top.number == nil) != (marker.number == nil) { break }
                        while let last = indents.last, last > marker.indent { indents.removeLast() }
                        if indents.last != marker.indent {
                            // Deeper than the item above (or the very first): one more level, never skipping.
                            indents.append(marker.indent)
                        }
                        var text = marker.text
                        var checked: Bool?
                        if text.hasPrefix("[ ] ") || text == "[ ]" { checked = false; text = String(text.dropFirst(3)).trimmingCharacters(in: .whitespaces) }
                        else if text.lowercased().hasPrefix("[x] ") || text.lowercased() == "[x]" { checked = true; text = String(text.dropFirst(3)).trimmingCharacters(in: .whitespaces) }
                        items.append(ListItem(level: indents.count - 1, number: marker.number, checked: checked, text: text))
                        blankBefore = false
                        i += 1
                        continue
                    }
                    // Anything that opens its own block ends the list; so does unindented text after a blank line.
                    let indent = leadingWidth(current)
                    if fenceOpen(t) != nil || heading(t) != nil || isRule(t) || t.hasPrefix(">") { break }
                    if blankBefore && indent < 2 { break }
                    if t.contains("|"), i + 1 < lines.endIndex, delimiterRow(lines[i + 1]) != nil { break }
                    // Continuation of the item above.
                    items[items.count - 1].text += (blankBefore ? "\n\n" : "\n") + t
                    blankBefore = false
                    i += 1
                }
                blocks.append(.list(items))
                continue
            }

            paragraph.append(trimmed)
            i += 1
        }
        flush()
        return blocks
    }

    private struct Fence { var marker: String; var language: String? }

    private static func fenceOpen(_ trimmed: String) -> Fence? {
        for ch: Character in ["`", "~"] {
            let run = trimmed.prefix { $0 == ch }
            guard run.count >= 3 else { continue }
            let info = trimmed.dropFirst(run.count).trimmingCharacters(in: .whitespaces)
            if ch == "`", info.contains("`") { return nil }
            return Fence(marker: String(run), language: info.isEmpty ? nil : String(info.split(separator: " ").first ?? ""))
        }
        return nil
    }

    private static func heading(_ trimmed: String) -> Block? {
        let hashes = trimmed.prefix { $0 == "#" }
        guard (1...6).contains(hashes.count) else { return nil }
        let rest = trimmed.dropFirst(hashes.count)
        guard rest.isEmpty || rest.first == " " else { return nil }
        var text = rest.trimmingCharacters(in: .whitespaces)
        // Optional closing hashes: "## 제목 ##".
        if let range = text.range(of: #"\s+#+$"#, options: .regularExpression) { text.removeSubrange(range) }
        else if text.allSatisfy({ $0 == "#" }) { text = "" }
        return .heading(level: hashes.count, text: text)
    }

    private static func isRule(_ trimmed: String) -> Bool {
        guard let first = trimmed.first, "-*_".contains(first) else { return false }
        let chars = trimmed.filter { $0 != " " }
        return chars.count >= 3 && chars.allSatisfy { $0 == first }
    }

    private struct Marker { var indent: Int; var number: String?; var text: String }

    private static func listMarker(_ line: String) -> Marker? {
        let indent = leadingWidth(line)
        let body = line.drop { $0 == " " || $0 == "\t" }
        guard let first = body.first else { return nil }
        if "-*+".contains(first) {
            let rest = body.dropFirst()
            guard rest.isEmpty || rest.first == " " else { return nil }
            if isRule(String(body)) { return nil }
            return Marker(indent: indent, number: nil, text: rest.trimmingCharacters(in: .whitespaces))
        }
        let digits = body.prefix { $0.isASCII && $0.isNumber }
        guard (1...9).contains(digits.count) else { return nil }
        let after = body.dropFirst(digits.count)
        guard let delimiter = after.first, delimiter == "." || delimiter == ")" else { return nil }
        let rest = after.dropFirst()
        guard rest.isEmpty || rest.first == " " else { return nil }
        return Marker(indent: indent, number: "\(digits)\(delimiter)", text: rest.trimmingCharacters(in: .whitespaces))
    }

    private static func leadingWidth(_ line: String) -> Int {
        var width = 0
        for ch in line {
            if ch == " " { width += 1 } else if ch == "\t" { width += 4 } else { break }
        }
        return width
    }

    /// `| --- | :---: | ---: |` → alignments, or nil when the line is not a delimiter row.
    private static func delimiterRow(_ line: String) -> [Alignment]? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard trimmed.contains("-"), trimmed.contains("|"), trimmed.allSatisfy({ "|-: ".contains($0) }) else { return nil }
        let parts = cells(trimmed)
        var result: [Alignment] = []
        for part in parts {
            // While streaming, the last cell may still be empty ("|---|").
            guard part.contains("-") || part.isEmpty else { return nil }
            let left = part.hasPrefix(":"), right = part.hasSuffix(":") && part.count > 1
            result.append(left && right ? .center : right ? .trailing : .leading)
        }
        return result
    }

    /// Cells of a table row: split on `|` outside code spans and `[[link|title]]` links, with `\|` kept as a pipe.
    static func cells(_ row: String) -> [String] {
        var s = Substring(row.trimmingCharacters(in: .whitespaces))
        if s.first == "|" { s = s.dropFirst() }
        if s.last == "|", !s.hasSuffix("\\|") { s = s.dropLast() }
        var cells: [String] = []
        var current = ""
        var inCode = false
        var escaped = false
        // Inside `[[…]]` the pipe divides the link from its title, not the cells.
        var inLink = false
        for ch in s {
            if escaped { current.append(ch == "|" ? "|" : "\\\(ch)"); escaped = false; continue }
            if ch == "\\" { escaped = true; continue }
            if ch == "`" { inCode.toggle() }
            if !inCode, ch == "[", current.hasSuffix("[") { inLink = true }
            if !inCode, ch == "]", current.hasSuffix("]") { inLink = false }
            if ch == "|", !inCode, !inLink {
                cells.append(current.trimmingCharacters(in: .whitespaces))
                current = ""
            } else {
                current.append(ch)
            }
        }
        if escaped { current.append("\\") }
        cells.append(current.trimmingCharacters(in: .whitespaces))
        return cells
    }

    // MARK: - Inline

    /// Inline Markdown (bold, italic, code, links, `~~strike~~`) with `[[gistra://…|제목]]` as links.
    /// A lone `~` ("낮음~중간") stays a tilde, and `$…$` math is left exactly as written.
    static func inline(_ text: String) -> AttributedString {
        let source = inlineSource(text)
        return (try? AttributedString(markdown: source, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace,
                                                                      failurePolicy: .returnPartiallyParsedIfPossible)))
            ?? AttributedString(text)
    }

    /// The Markdown actually handed to Foundation: wiki links rewritten, single tildes and math escaped.
    static func inlineSource(_ text: String) -> String {
        // A title may hold single brackets ("[필독] 공지"): only "]]" ends the link, and the brackets are escaped so
        // the Markdown link text stays one piece.
        let linked = text.replacing(/\[\[(gistra:\/\/[^\]|]+)(?:\|((?:[^\]]|\](?!\]))+))?\]\]/) { m in
            let label = (m.2.map(String.init) ?? "링크").replacingOccurrences(of: "[", with: "\\[").replacingOccurrences(of: "]", with: "\\]")
            return "[\(label)](\(m.1))"
        }
        let chars = Array(linked)
        var out = ""
        out.reserveCapacity(chars.count + 8)
        var i = 0
        while i < chars.count {
            let ch = chars[i]
            switch ch {
            case "\\":
                out.append(ch)
                if i + 1 < chars.count { out.append(chars[i + 1]) }
                i += 2
            case "`":
                // A code span is copied verbatim up to its matching backtick run.
                var run = 0
                while i + run < chars.count, chars[i + run] == "`" { run += 1 }
                if let close = closingBackticks(chars, from: i + run, count: run) {
                    out.append(contentsOf: chars[i..<(close + run)])
                    i = close + run
                } else {
                    out.append(contentsOf: chars[i..<(i + run)])
                    i += run
                }
            case "~":
                var run = 0
                while i + run < chars.count, chars[i + run] == "~" { run += 1 }
                if run == 2 { out.append("~~") } else { out.append(String(repeating: "\\~", count: run)) }
                i += run
            case "$":
                if let end = mathEnd(chars, from: i) {
                    for c in chars[i...end] {
                        if "\\*_~`[]<>#!".contains(c) { out.append("\\") }
                        out.append(c)
                    }
                    i = end + 1
                } else {
                    out.append(ch)
                    i += 1
                }
            default:
                out.append(ch)
                i += 1
            }
        }
        return out
    }

    private static func closingBackticks(_ chars: [Character], from start: Int, count: Int) -> Int? {
        var j = start
        while j < chars.count {
            if chars[j] == "`" {
                var run = 0
                while j + run < chars.count, chars[j + run] == "`" { run += 1 }
                if run == count { return j }
                j += run
            } else {
                j += 1
            }
        }
        return nil
    }

    /// The index of the closing `$` of a math span starting at `start`, or nil for prices like "$5 and $10".
    private static func mathEnd(_ chars: [Character], from start: Int) -> Int? {
        let display = start + 1 < chars.count && chars[start + 1] == "$"
        let open = start + (display ? 2 : 1)
        guard open < chars.count, !chars[open].isWhitespace else { return nil }
        var j = open
        while j < chars.count {
            let c = chars[j]
            if c == "\\" { j += 2; continue }
            if !display, c == "\n" { return nil }
            if c == "$" {
                if display {
                    guard j + 1 < chars.count, chars[j + 1] == "$" else { j += 1; continue }
                    return j + 1
                }
                guard !chars[j - 1].isWhitespace else { return nil }
                if j + 1 < chars.count, chars[j + 1].isNumber { return nil }
                return j
            }
            j += 1
        }
        return nil
    }
}
