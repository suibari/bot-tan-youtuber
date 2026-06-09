using UnityEngine;
using UniVRM10;
using System.Collections.Generic;
using System.IO;

[RequireComponent(typeof(AudioSource))]
public class VRM1LipSync : MonoBehaviour
{
    public float silenceThreshold = 0.005f;
    public float maxVolume = 0.3f;
    [Range(0f, 1f)] public float maxWeight = 1.0f;
    public float smoothing = 0.05f;
    public float emotionWeight = 0.6f;  // 表情の強さ

    private AudioSource _audioSource;
    private float _currentWeight = 0f;
    private float[] _samples = new float[256];
    private Vrm10RuntimeExpression _expression;

    // 感情タイムライン
    private List<EmotionEntry> _emotions = new List<EmotionEntry>();
    private int _emotionIndex = 0;
    private ExpressionKey _currentEmotion = ExpressionKey.Happy;

    // Waveタイムライン
    private float _introWaveTime = 0f;
    private bool  _introWaveFired = false;
    private float _waveTime = 0f;
    private bool  _waveFired = false;

    [System.Serializable]
    private class EmotionEntry
    {
        public float time;
        public string emotion;
    }

    void Start()
    {
        _audioSource = GetComponent<AudioSource>();
        var vrm = GetComponent<Vrm10Instance>() ?? GetComponentInParent<Vrm10Instance>();
        if (vrm != null)
            _expression = vrm.Runtime.Expression;
        else
            Debug.LogError("[LipSync] Vrm10Instance not found");

        LoadEmotions();
    }

    void LoadEmotions()
    {
        string emotionFile = GetArg("-emotionFile");
        if (string.IsNullOrEmpty(emotionFile) || !File.Exists(emotionFile))
        {
            Debug.Log("[LipSync] 感情ファイルなし、Happyで固定");
            return;
        }
        string json = File.ReadAllText(emotionFile);
        var data = JsonUtility.FromJson<EmotionData>(json);
        _emotions = new List<EmotionEntry>(data.emotions);
        _introWaveTime = data.introWaveTime;
        _waveTime = data.waveTime;
        Debug.Log($"[LipSync] 感情タイムライン: {_emotions.Count}件, introWaveTime: {_introWaveTime}s, waveTime: {_waveTime}s");
    }

    [System.Serializable]
    private class EmotionData
    {
        public EmotionEntry[] emotions;
        public float introWaveTime;
        public float waveTime;
    }

    string GetArg(string name)
    {
        var args = System.Environment.GetCommandLineArgs();
        for (int i = 0; i < args.Length - 1; i++)
            if (args[i] == name) return args[i + 1];
        return null;
    }

    ExpressionKey ToExpressionKey(string emotion) => emotion switch
    {
        "Happy"     => ExpressionKey.Happy,
        "Sad"       => ExpressionKey.Sad,
        "Angry"     => ExpressionKey.Angry,
        "Surprised" => ExpressionKey.Surprised,
        "Relaxed"   => ExpressionKey.Relaxed,
        _           => ExpressionKey.Happy,
    };

    float GetEmotionWeight(ExpressionKey key)
    {
        if (key.Equals(ExpressionKey.Happy)) return 0.5f;
        return emotionWeight; // 他はInspectorの設定値（デフォルト0.6f）
    }

    void LateUpdate()
    {
        if (_audioSource == null || _expression == null) return;

        // 感情タイムライン更新
        if (_audioSource.isPlaying && _emotions.Count > 0)
        {
            float t = _audioSource.time;
            while (_emotionIndex < _emotions.Count - 1
                   && t >= _emotions[_emotionIndex + 1].time)
                _emotionIndex++;
            _currentEmotion = ToExpressionKey(_emotions[_emotionIndex].emotion);
        }

        // 表情セット（口パクと競合しないよう Aa 以外をセット）
        _expression.SetWeight(ExpressionKey.Happy,     0f);
        _expression.SetWeight(ExpressionKey.Sad,       0f);
        _expression.SetWeight(ExpressionKey.Angry,     0f);
        _expression.SetWeight(ExpressionKey.Surprised, 0f);
        _expression.SetWeight(ExpressionKey.Relaxed,   0f);
        _expression.SetWeight(_currentEmotion, GetEmotionWeight(_currentEmotion));

        // 口パク
        if (!_audioSource.isPlaying)
        {
            _currentWeight = Mathf.Lerp(_currentWeight, 0f, Time.deltaTime / smoothing);
            _expression.SetWeight(ExpressionKey.Aa, _currentWeight);
            return;
        }

        // 冒頭Waving発火（冒頭一言終了 → 挨拶アニメへ）
        if (!_introWaveFired && _introWaveTime > 0 && _audioSource.isPlaying
            && _audioSource.time >= _introWaveTime)
        {
            var animator = GetComponentInParent<Animator>();
            if (animator != null) animator.SetTrigger("DoWave");
            _introWaveFired = true;
            Debug.Log($"[LipSync] 冒頭DoWave発火: {_audioSource.time}s");
        }

        // 締めWaving発火
        if (!_waveFired && _waveTime > 0 && _audioSource.isPlaying
            && _audioSource.time >= _waveTime)
        {
            var animator = GetComponentInParent<Animator>();
            if (animator != null) animator.SetTrigger("DoWave");
            _waveFired = true;
            Debug.Log($"[LipSync] 締めDoWave発火: {_audioSource.time}s");
        }

        _audioSource.GetOutputData(_samples, 0);
        float rms = 0f;
        foreach (var s in _samples) rms += s * s;
        rms = Mathf.Sqrt(rms / _samples.Length);

        float targetWeight = rms > silenceThreshold
            ? Mathf.Clamp01((rms - silenceThreshold) / (maxVolume - silenceThreshold)) * maxWeight
            : 0f;

        _currentWeight = Mathf.Lerp(_currentWeight, targetWeight, Time.deltaTime / smoothing);
        _expression.SetWeight(ExpressionKey.Aa, _currentWeight);
    }
}
