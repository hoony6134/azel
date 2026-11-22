import SwiftUI
import WidgetKit

/// One tap to call Azel: the iPhone Home/Lock Screen widget and the Apple Watch complication.
/// Static (nothing to refresh); the tap opens azel://call, which the app turns into a call.
struct CallWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "xyz.scian.biseo.call", provider: CallWidgetProvider()) { _ in
            CallWidgetView()
                .widgetURL(URL(string: "azel://call"))
                .containerBackground(for: .widget) { Color.black }
        }
        .configurationDisplayName("Azel 부르기")
        .description("탭하면 Azel에게 전화를 걸어요.")
        .supportedFamilies(Self.families)
    }

    #if os(watchOS)
    static let families: [WidgetFamily] = [.accessoryCircular, .accessoryCorner, .accessoryRectangular, .accessoryInline]
    #else
    static let families: [WidgetFamily] = [.systemSmall, .systemMedium, .accessoryCircular, .accessoryRectangular, .accessoryInline]
    #endif
}

struct CallWidgetProvider: TimelineProvider {
    func placeholder(in context: Context) -> SimpleEntry { SimpleEntry() }
    func getSnapshot(in context: Context, completion: @escaping (SimpleEntry) -> Void) { completion(SimpleEntry()) }
    func getTimeline(in context: Context, completion: @escaping (Timeline<SimpleEntry>) -> Void) {
        completion(Timeline(entries: [SimpleEntry()], policy: .never))
    }
}

struct SimpleEntry: TimelineEntry { var date = Date() }

private let celadon = Color(red: 0x82 / 255, green: 0xCB / 255, blue: 0xB4 / 255)

struct CallWidgetView: View {
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .accessoryInline:
            Label("Azel 부르기", systemImage: "phone.fill")
        case .accessoryRectangular:
            HStack {
                Image(systemName: "phone.fill").font(.title3)
                VStack(alignment: .leading) {
                    Text("Azel").font(.headline)
                    Text("탭해서 부르기").font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
        case .systemSmall:
            VStack(spacing: 10) {
                orb(64)
                Label("Azel 부르기", systemImage: "phone.fill").font(.subheadline.weight(.semibold)).foregroundStyle(.white)
            }
        case .systemMedium:
            HStack(spacing: 16) {
                orb(84)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Azel").font(.title2.weight(.semibold)).foregroundStyle(.white)
                    Text("탭하면 바로 전화를 걸어요").font(.subheadline).foregroundStyle(.white.opacity(0.7))
                }
                Spacer(minLength: 0)
                Image(systemName: "phone.fill")
                    .font(.title3)
                    .foregroundStyle(.black)
                    .frame(width: 48, height: 48)
                    .background(Circle().fill(celadon))
                    .accessibilityHidden(true)
            }
            .padding(.horizontal, 4)
        default:   // circular, corner
            ZStack {
                AccessoryWidgetBackground()
                Image(systemName: "phone.fill").font(.title3).widgetAccentable()
            }
            .accessibilityLabel("Azel 부르기")
            #if os(watchOS)
            .widgetLabel("Azel")
            #endif
        }
    }

    private func orb(_ size: CGFloat) -> some View {
        Circle()
            .fill(RadialGradient(colors: [.white.opacity(0.9), celadon, celadon.opacity(0.75)],
                                 center: UnitPoint(x: 0.38, y: 0.32), startRadius: 0, endRadius: size * 0.6))
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}
