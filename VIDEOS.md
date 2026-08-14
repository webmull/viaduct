# Site videos

How the explainer videos on the site are produced. They are self-hosted under
`site/videos/`, embedded as lazy-loaded `<video>`, and served by Caddy with range
support (so scrubbing works). Committed to the repo because the deploy flow is
`git reset --hard origin/main` + `cp -r site/*` onto each node.

Current videos:

- `site/videos/intro.mp4` — homepage, below the hero.
- `site/videos/holding-page.mp4` — top of `/articles/name-your-tunnel/`.
- `*-poster.jpg` — the poster frame (the white logo opener).

Each opens on the white `viaduct.sh` logo, crossfades into a screen recording
(webcam picture-in-picture bottom-right + terminal + the local "Beacon" demo app),
has normalised audio, and burned-in captions.

## Tools

- **A full ffmpeg with libass.** The Homebrew `ffmpeg` here is a minimal build with
  no libass/freetype, so it has no `subtitles`/`ass`/`drawtext` filters. Use a full
  static build, e.g. from evermeet.cx:
  ```sh
  curl -L https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip -o ff.zip && unzip ff.zip
  ```
- **whisper-cpp** for transcription: `brew install whisper-cpp`, plus the `base.en`
  model (`ggml-base.en.bin` from huggingface.co/ggerganov/whisper.cpp).
- **Headless Chrome** to render the white opener frame with real DM Sans.
- **`demo-app.py`** (repo root, untracked): a stdlib "Beacon" dashboard used as the
  thing being tunnelled. Run it, then `viaduct http 8080`, and record.

## Pipeline

### 1. Record
1080p/30, circular webcam PiP bottom-right, tunnelling the Beacon demo app. Keep the
face in the corner so captions can sit bottom-left, clear of it.

### 2. Opener (headless Chrome)
A 1920x1080 white page with the black viaduct mark + `viaduct.sh` (DM Sans), rendered
to a PNG with Chrome, then a ~2.2s clip that fades in from white with a silent audio
track:
```sh
ffmpeg -loop 1 -i opener.png -f lavfi -i anullsrc=r=48000:cl=stereo -t 2.2 \
  -vf "fade=in:color=white:st=0:d=0.6,fps=30,format=yuv420p,setsar=1" \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest opener.mp4
```

### 3. Crossfade + audio normalise (the "clean final")
Crossfade the opener into the recording (1.2s), and fix the audio: the raw recordings
are very quiet (~-43 LUFS), so high-pass and loudness-normalise to -16 LUFS.
```sh
ffmpeg -i opener.mp4 -i recording.mp4 -filter_complex "
  [0:v]fps=30,scale=1920:1080,setsar=1,format=yuv420p,settb=AVTB[v0];
  [1:v]fps=30,scale=1920:1080,setsar=1,format=yuv420p,settb=AVTB[v1];
  [v0][v1]xfade=transition=fade:duration=1.2:offset=1.0[v];
  [1:a]highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a1];
  [0:a]aresample=48000[a0];
  [a0][a1]acrossfade=d=1.2[a]" \
  -map "[v]" -map "[a]" -c:v libx264 -crf 21 -c:a aac -b:a 160k clean.mp4
```
This `clean.mp4` (opener + recording + good audio, no captions) is the master to burn
onto. **Keep it.**

### 4. Poster
Use the opener frame as the poster (a branded still, not a mid-video grab).

### 5. Captions (burned in)
Native `<track>` VTT was tried first and dropped: iOS mis-positions native captions
(mid-screen, over the camera). So captions are **burned into the picture**, which
looks identical on every device including iOS fullscreen.

1. Transcribe: extract 16 kHz mono wav, run whisper-cpp `base.en`, output VTT.
2. **Hand-correct** the VTT (whisper mangles "viaduct", subdomains, and CLI flags).
3. Convert VTT to ASS and force the resolution and style:
   - `PlayResX: 1920`, `PlayResY: 1080` in `[Script Info]` (or fonts scale ~5x).
   - `WrapStyle: 1` so lines fill the width before wrapping.
   - Style: `Helvetica, 60px`, white text, **`BorderStyle=4` with a semi-transparent
     grey box** (`BackColour=&H59303030`) so it reads on both white and dark frames,
     `Alignment=1` (bottom-left), `MarginR ~395` so lines stop before the ~82%
     bottom-right PiP, `MarginV=58`.
   - Delay the first cue to ~2.3s so nothing sits over the white opener.
   The style line:
   ```
   Style: Default,Helvetica,60,&H00FFFFFF,&H00FFFFFF,&H00101010,&H59303030,0,0,0,0,100,100,0,0,4,6,0,1,64,395,58,1
   ```
4. Burn (video re-encoded, audio copied):
   ```sh
   ffmpeg -i clean.mp4 -vf "ass=captions.ass" -c:a copy -c:v libx264 -crf 21 \
     -preset medium -movflags +faststart site/videos/NAME.mp4
   ```

**Never burn onto an already-burned mp4.** Rebuild `clean.mp4` (step 3) from the
source recording first, then burn.

### 6. Embed + deploy
```html
<div class="video-frame">
  <video controls preload="none" playsinline poster="/videos/NAME-poster.jpg">
    <source src="/videos/NAME.mp4" type="video/mp4" />
  </video>
</div>
```
`.video-frame` is styled in `site/styles.css`. Commit the `.mp4` and poster, then
deploy the site to the fleet (site float). Caddy already serves `.mp4` with
`Accept-Ranges: bytes`.
