' Azel TV: a view-only dashboard. Entry point.
' Launch parameters (from provision.sh, `POST :8060/launch/dev?azelUrl=...&azelToken=...`) are saved once; the
' settings screen (remote's * key) does the same by hand.
sub Main(args as Object)
    if args <> invalid
        if args.azelUrl <> invalid and args.azelToken <> invalid
            reg = CreateObject("roRegistrySection", "azel")
            reg.Write("url", args.azelUrl)
            reg.Write("token", args.azelToken)
            reg.Flush()
        end if
    end if
    screen = CreateObject("roSGScreen")
    port = CreateObject("roMessagePort")
    screen.SetMessagePort(port)
    screen.CreateScene("MainScene")
    screen.Show()
    while true
        msg = wait(0, port)
        if type(msg) = "roSGScreenEvent"
            if msg.IsScreenClosed() then return
        end if
    end while
end sub
