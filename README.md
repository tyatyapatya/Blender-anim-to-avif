# Blender anim to AVIF

Render an animation to a single `.avif` file from Blender. Supports transparency, looping and adjustable quality. The Windows encoder is included.

Windows x64, Blender 4.2+. Tested with Blender 4.5 and 5.0.

## Install

Download the Windows ZIP from the [latest release](https://github.com/tyatyapatya/Blender-anim-to-avif/releases/latest). Choose the ZIP under Assets, not the source code archive.

In Blender, go to **Edit → Preferences → Add-ons → Install from Disk** and select the ZIP.

## Use

Set your frame range and frame rate, then open **Output Properties → AVIF Animation**. Choose an output path and click **Render & Export AVIF**. Save your `.blend` first if using a relative path like `//animation.avif`.

Open the result in Edge, Chrome or Firefox to play it. Windows Photos may only show the first frame.

The add-on is GPL-3.0-or-later. Bundled encoder licenses are in `licenses/`.
