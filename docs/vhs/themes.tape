Output "docs/assets/themes.gif"

Source "docs/vhs/_common.tape"
Set Framerate 24

# Launch the demo off-camera and let the map fill in.
Hide
Type "amtr --demo"
Enter
Sleep 4s
Show

# Cycle the block themes: terminal (default) → ukiyo-e → bauhaus → mono,
# holding each long enough to read the map, then land back on terminal.
Sleep 2500ms
Type "t"
Sleep 2500ms
Type "t"
Sleep 2500ms
Type "t"
Sleep 2500ms
Type "t"
Sleep 2s
