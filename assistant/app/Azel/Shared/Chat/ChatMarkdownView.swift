import SwiftUI

// Ported from GiSTRA (ChatMarkdownView.swift, the same author's code): Azel's Theme, plus a copy button on code.

/// An AI reply as Markdown blocks (headings, lists, code, quotes, tables, rules), parsed by `ChatMarkdown`.
/// Equatable on the text so a reply is parsed only when it changes: the others keep theirs while one streams in.
struct ChatMarkdownView: View, Equatable {
    let text: String

    nonisolated static func == (a: Self, b: Self) -> Bool { a.text == b.text }

    var body: some View {
        ChatBlocks(blocks: ChatMarkdown.parse(text))
            .font(.body).foregroundStyle(Theme.text)
            .tint(Theme.celadon)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ChatBlocks: View {
    let blocks: [ChatMarkdown.Block]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                ChatBlockView(block: block)
            }
        }
    }
}

private struct ChatBlockView: View {
    let block: ChatMarkdown.Block

    var body: some View {
        switch block {
        case .paragraph(let text):
            ChatInline(text).lineSpacing(4)
        case .heading(let level, let text):
            ChatInline(text).font(level == 1 ? .title3.weight(.semibold) : level == 2 ? .headline : .body.weight(.semibold))
                .padding(.top, level <= 2 ? 4 : 0)
        case .list(let items):
            VStack(alignment: .leading, spacing: 5) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    ChatListRow(item: item)
                }
            }
        case .code(let language, let text):
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    if let language { Text(language).font(.caption).foregroundStyle(Theme.text3) }
                    Spacer()
                    CodeCopyButton(text: text)
                }
                ScrollView(.horizontal) {
                    Text(text).font(.system(.callout, design: .monospaced)).fixedSize(horizontal: true, vertical: false)
                }
                .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
                .scrollIndicators(.hidden)
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.bgRaised, in: .rect(cornerRadius: Theme.rowRadius))
        case .quote(let blocks):
            ChatBlocks(blocks: blocks)
                .foregroundStyle(Theme.text2)
                .padding(.leading, 12)
                .overlay(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 1.5).fill(Theme.stroke).frame(width: 3)
                }
        case .table(let table):
            ChatTable(table: table)
        case .rule:
            Rectangle().fill(Theme.stroke).frame(height: 1).padding(.vertical, 2)
        }
    }
}

/// Inline Markdown in one `Text`; code spans get a field-coloured background.
private struct ChatInline: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text(Self.styled(text)).fixedSize(horizontal: false, vertical: true)
    }

    static func styled(_ text: String) -> AttributedString {
        var string = ChatMarkdown.inline(text)
        for run in string.runs where run.inlinePresentationIntent?.contains(.code) == true {
            string[run.range].backgroundColor = Theme.bgRaised
        }
        return string
    }
}

private struct ChatListRow: View {
    let item: ChatMarkdown.ListItem

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Group {
                if let checked = item.checked {
                    Image(systemName: checked ? "checkmark.square.fill" : "square")
                        .foregroundStyle(checked ? Theme.celadon : Theme.text3)
                } else if let number = item.number {
                    Text(number).monospacedDigit().foregroundStyle(Theme.text2)
                } else {
                    Text(["•", "◦", "▪︎"][item.level % 3]).foregroundStyle(Theme.text2)
                }
            }
            .frame(minWidth: 14, alignment: .trailing)
            ChatInline(item.text).lineSpacing(4)
        }
        .padding(.leading, CGFloat(item.level) * 18)
    }
}

/// A GitHub table: header row, column and row dividers, scrolling sideways when wider than the chat.
private struct ChatTable: View {
    let table: ChatMarkdown.Table

    var body: some View {
        let widths = columnWidths
        ScrollView(.horizontal) {
            Grid(alignment: .topLeading, horizontalSpacing: 0, verticalSpacing: 0) {
                row(table.header, widths: widths, header: true)
                ForEach(Array(table.rows.enumerated()), id: \.offset) { _, cells in
                    Rectangle().fill(Theme.stroke).frame(height: 1).gridCellUnsizedAxes(.horizontal)
                    row(cells, widths: widths, header: false)
                }
            }
            .overlay(RoundedRectangle(cornerRadius: Theme.rowRadius).strokeBorder(Theme.stroke))
            .clipShape(.rect(cornerRadius: Theme.rowRadius))
        }
        .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
    }

    private func row(_ cells: [String], widths: [CGFloat], header: Bool) -> some View {
        GridRow {
            ForEach(Array(cells.enumerated()), id: \.offset) { column, cell in
                ChatInline(cell)
                    .font(header ? .body.weight(.semibold) : .body)
                    .multilineTextAlignment(textAlignment(column))
                    .frame(width: widths[column], alignment: frameAlignment(column))
                    .padding(.horizontal, 10).padding(.vertical, 7)
                    .frame(maxHeight: .infinity, alignment: .top)
                    .background(header ? Theme.bgRaised : .clear)
                    .overlay(alignment: .trailing) {
                        if column < cells.count - 1 { Rectangle().fill(Theme.stroke).frame(width: 1) }
                    }
            }
        }
    }

    /// Text width estimated from characters (Hangul and other wide glyphs count double), clamped so one long cell
    /// wraps instead of stretching the table.
    private var columnWidths: [CGFloat] {
        (0..<table.header.count).map { column in
            let longest = ([table.header] + table.rows).map { Self.estimate($0[column]) }.max() ?? 0
            return min(max(longest, 28), 220)
        }
    }

    private static func estimate(_ cell: String) -> CGFloat {
        // Markup characters are skipped; a link's target still counts, which only makes its column a bit wider.
        let units = cell.unicodeScalars.reduce(0.0) { sum, scalar in
            "*_`~[]\\".unicodeScalars.contains(scalar) ? sum : sum + (scalar.value < 0x1100 ? 0.58 : 1.0)
        }
        return ceil(CGFloat(units) * 14.5) + 2
    }

    private func frameAlignment(_ column: Int) -> SwiftUI.Alignment {
        switch table.alignments[column] {
        case .leading: .leading
        case .center: .center
        case .trailing: .trailing
        }
    }

    private func textAlignment(_ column: Int) -> TextAlignment {
        switch table.alignments[column] {
        case .leading: .leading
        case .center: .center
        case .trailing: .trailing
        }
    }
}

/// Copies a code block; "복사됨" for a moment.
private struct CodeCopyButton: View {
    let text: String
    @State private var copied = false
    var body: some View {
        Button {
            #if os(macOS)
            NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
            #else
            UIPasteboard.general.string = text
            #endif
            copied = true
            Task { try? await Task.sleep(nanoseconds: 1_500_000_000); copied = false }
        } label: {
            Label(copied ? "복사됨" : "복사", systemImage: copied ? "checkmark" : "doc.on.doc").font(.caption)
        }
        .buttonStyle(.borderless).foregroundStyle(Theme.text2)
        .accessibilityLabel(copied ? "복사됨" : "코드 복사")
    }
}
