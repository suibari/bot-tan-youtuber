using UnityEngine;
using UnityEditor;
using UnityEditor.Recorder;
using UnityEditor.Recorder.Input;
using System.Collections;
using System.IO;

[InitializeOnLoad]
public class VideoRecorderAutoPlay
{
    static VideoRecorderAutoPlay()
    {
        Debug.Log("[AutoPlay] InitializeOnLoad triggered");
        string wavPath = GetArg("-wavFile");
        Debug.Log($"[AutoPlay] wavPath: {wavPath}");
        if (!string.IsNullOrEmpty(wavPath))
        {
            EditorApplication.update += TryEnterPlaymode;
        }
    }

    static void TryEnterPlaymode()
    {
        if (!EditorApplication.isPlayingOrWillChangePlaymode)
        {
            EditorApplication.update -= TryEnterPlaymode;
            EditorApplication.EnterPlaymode();
        }
    }

    public static string GetArg(string name)
    {
        var args = System.Environment.GetCommandLineArgs();
        for (int i = 0; i < args.Length; i++)
            if (args[i] == name && i + 1 < args.Length)
                return args[i + 1];
        return null;
    }
}

public class VideoRecorder : MonoBehaviour
{
    [SerializeField] private AudioSource audioSource;
    [SerializeField] private string screenshotPath = "";

    void Start()
    {
        string wavPath = VideoRecorderAutoPlay.GetArg("-wavFile");
        string outPath = VideoRecorderAutoPlay.GetArg("-outputFile");
        string outputPath = string.IsNullOrEmpty(outPath) ? "/tmp/bottan_output" : outPath;

        StartCoroutine(RecordVideo(wavPath, outputPath));
    }

    IEnumerator RecordVideo(string wavPath, string outputPath)
    {
        // スクリーンショットパスを引数から取得
        screenshotPath = VideoRecorderAutoPlay.GetArg("-screenshotFile") ?? "";

        if (!string.IsNullOrEmpty(wavPath) && File.Exists(wavPath))
        {
            yield return StartCoroutine(LoadAudioClip(wavPath));
        }

        // スクリーンショット撮影（録画開始前）
        if (!string.IsNullOrEmpty(screenshotPath))
        {
            yield return new WaitForEndOfFrame();
            ScreenCapture.CaptureScreenshot(screenshotPath);
            yield return new WaitForSeconds(0.5f); // 書き込み待ち
            Debug.Log($"[Screenshot] 保存: {screenshotPath}");
        }

        if (!string.IsNullOrEmpty(wavPath) && File.Exists(wavPath))
        {
            yield return StartCoroutine(LoadAudioClip(wavPath));
        }

        var controllerSettings = ScriptableObject.CreateInstance<RecorderControllerSettings>();
        var recorderController = new RecorderController(controllerSettings);

        var movieRecorder = ScriptableObject.CreateInstance<MovieRecorderSettings>();
        movieRecorder.name = "BotTan Recorder";
        movieRecorder.Enabled = true;
        movieRecorder.OutputFormat = MovieRecorderSettings.VideoRecorderOutputFormat.WebM;
        movieRecorder.AudioInputSettings.PreserveAudio = true;
        movieRecorder.OutputFile = outputPath.Replace(".webm", "");
        movieRecorder.ImageInputSettings = new GameViewInputSettings
        {
            OutputWidth = 1080,
            OutputHeight = 1920
        };

        controllerSettings.AddRecorderSettings(movieRecorder);
        controllerSettings.SetRecordModeToManual();
        controllerSettings.FrameRate = 30;

        RecorderOptions.VerboseMode = false;
        recorderController.PrepareRecording();
        recorderController.StartRecording();

        if (audioSource != null && audioSource.clip != null)
        {
            audioSource.Play();
            float clipLength = audioSource.clip.length;
            Debug.Log($"[Recorder] clip length: {clipLength}s");
            yield return new WaitForSeconds(clipLength + 3.0f);
        }
        else
        {
            yield return new WaitForSeconds(5f);
        }

        recorderController.StopRecording();
        Debug.Log($"Recording complete: {outputPath}.webm");

        #if UNITY_EDITOR
            EditorApplication.ExitPlaymode();
        #else
            Application.Quit();
        #endif
    }

    IEnumerator LoadAudioClip(string path)
    {
        string url = "file://" + path;
        using (var www = UnityEngine.Networking.UnityWebRequestMultimedia.GetAudioClip(url, AudioType.WAV))
        {
            yield return www.SendWebRequest();

            if (www.result == UnityEngine.Networking.UnityWebRequest.Result.Success)
            {
                audioSource.clip = UnityEngine.Networking.DownloadHandlerAudioClip.GetContent(www);
            }
            else
            {
                Debug.LogError($"Failed to load audio: {www.error}");
            }
        }
    }
}
