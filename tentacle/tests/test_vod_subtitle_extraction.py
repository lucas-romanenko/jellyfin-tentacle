"""A Tentacle VOD's embedded subtitles are cached only when Jellyfin's
ffmpeg read the whole file.

Jellyfin (SubtitleEncoder, 10.11) extracts an embedded text subtitle with one
ffmpeg over the whole file and keeps what it wrote for good, checking only
that the files are not empty. Tentacle closes a provider pull a few seconds
after the last viewer stopped (test_vod_viewers.py), and ffmpeg 7 then exits
0 as if the file had ended there -- measured: half a file served, 9 of 18
cues written, exit 0, with "Stream ends prematurely at 13054670, should be
26109340" and "Error during demuxing: Input/output error" in its log. Jellyfin
logged "extraction completed" for subtitles that stopped at minute 50 of a
140-minute film (2026-10-22), for every later play.

The plugin's `VodSubtitleExtractionPatch` (a Harmony prefix on
SubtitleEncoder.ExtractSubtitlesForFile) runs the same command for a Tentacle
VOD input into `<n>.partial.<ext>` and renames into place only after a clean,
whole read. There is no C# test host in this repo, so this reads the source
the way test_vod_hls_no_video_copy.py does.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

PATCH = Path("../tentacle-plugin/Services/VodSubtitleExtractionPatch.cs")
REGISTRATOR = Path("../tentacle-plugin/PluginServiceRegistrator.cs")


def method(src: str, signature: str) -> str:
    start = src.index(signature)
    rest = src[start:]
    end = re.search(r"\n    (?:private|public|protected|internal|/// )", rest[1:])
    return rest[: end.start() + 1] if end else rest


class TestSubtitleExtractionPatch(unittest.TestCase):
    def setUp(self):
        self.src = PATCH.read_text(encoding="utf-8")

    def test_it_is_registered_and_patches_jellyfins_extraction(self):
        self.assertIn("AddSingleton<IHostedService, VodSubtitleExtractionPatch>()",
                      REGISTRATOR.read_text(encoding="utf-8"))
        self.assertIn('"MediaBrowser.MediaEncoding.Subtitles.SubtitleEncoder"', self.src)
        self.assertIn('"ExtractSubtitlesForFile"', self.src)
        self.assertIn("UnpatchAll(HarmonyInstance.Id)", self.src, "an in-process restart must not patch twice")

    def test_only_a_tentacle_vod_input_is_taken_over(self):
        prefix = method(self.src, "private static bool Prefix(")
        self.assertIn("VodNoVideoCopyFilter.IsTentacleVodUrl(InputUrl(inputPath))", prefix)
        # every way out that is not ours lets Jellyfin's own method run
        self.assertGreaterEqual(prefix.count("return true;"), 3)
        self.assertEqual(1, prefix.count("return false;"))

    def test_a_cut_stream_is_recognised_by_what_ffmpeg_7_prints(self):
        errors = re.search(r"InputErrors\s*=\s*\{(.*?)\};", self.src, re.S).group(1)
        for line in ("[http @ 0x5b] Stream ends prematurely at 13054670, should be 26109340",
                     "[in#0/matroska,webm @ 0x5b] Error during demuxing: Input/output error"):
            self.assertTrue(any(e in line for e in re.findall(r'"([^"]+)"', errors)), line)

    def test_nothing_is_kept_unless_the_read_was_whole_and_clean(self):
        body = method(self.src, "private static async Task ExtractWhole(")
        self.assertIn("RedirectStandardError = true", body, "ffmpeg's log is what tells a cut stream")
        check = body[body.index("var missing"):body.index("for (var i = 0")]
        self.assertIn("exitCode != 0 || inputError.Length > 0 || missing.Count > 0", check)
        self.assertIn("partials.ForEach(DeleteQuietly)", check)
        self.assertIn("throw new FfmpegException", check)
        # renamed into Jellyfin's names only after that check
        self.assertLess(body.index("var missing"), body.index("File.Move(partials[i], outputPaths[i], true)"))

    def test_outputs_are_written_next_to_jellyfins_under_another_name(self):
        self.assertIn('Path.ChangeExtension(outputPath, ".partial" + Path.GetExtension(outputPath))', self.src)
        swap = method(self.src, "public static string? PartialArguments(")
        self.assertIn('var quoted = "\\"" + output + "\\"";', swap)
        self.assertIn("return null;", swap, "an argument layout it does not know is left to Jellyfin")


if __name__ == "__main__":
    unittest.main()
