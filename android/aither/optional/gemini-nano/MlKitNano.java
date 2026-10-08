package com.aitherium.aither;

import android.content.Context;

import com.google.common.util.concurrent.ListenableFuture;
import com.google.mlkit.genai.common.DownloadCallback;
import com.google.mlkit.genai.common.GenAiException;
import com.google.mlkit.genai.common.StreamingCallback;
import com.google.mlkit.genai.prompt.Candidate;
import com.google.mlkit.genai.prompt.GenerateContentResponse;
import com.google.mlkit.genai.prompt.Generation;
import com.google.mlkit.genai.prompt.java.GenerativeModelFutures;

import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * GeminiNano.Engine on ML Kit's GenAI Prompt API (com.google.mlkit:genai-prompt:1.0.0-beta4,
 * the Java futures surface). NOT in src/: the default build has no ML Kit, so this file is
 * compiled only by a build that carries it (README.md here says what that takes);
 * GeminiNano loads it by name and treats its absence as "no Nano on this phone".
 */
final class MlKitNano implements GeminiNano.Engine {
    private static final long STATUS_S = 10;
    private static final long ANSWER_S = 60;

    private final GenerativeModelFutures model;

    MlKitNano(Context c) {
        model = GenerativeModelFutures.from(Generation.INSTANCE.getClient());
    }

    @Override
    public int status() throws GeminiNano.Failure {
        Integer s = await(model.checkStatus(), STATUS_S);
        return s == null ? NanoRoute.UNAVAILABLE : s;
    }

    @Override
    public void download(GeminiNano.Progress p) {
        model.download(new DownloadCallback() {
            @Override public void onDownloadProgress(long done) { p.bytes(done); }
            @Override public void onDownloadCompleted() { p.done(); }
            @Override public void onDownloadFailed(GenAiException e) { p.failed(e.getErrorCode()); }
        });
    }

    @Override
    public String generate(String prompt, GeminiNano.Stream onText) throws GeminiNano.Failure {
        ListenableFuture<GenerateContentResponse> f = onText == null
                ? model.generateContent(prompt)
                : model.generateContent(prompt, new StreamingCallback() {
                    @Override public void onNewText(String chunk) { onText.text(chunk); }
                });
        GenerateContentResponse r = await(f, ANSWER_S);
        List<Candidate> cs = r == null ? null : r.getCandidates();
        if (cs == null || cs.isEmpty() || cs.get(0).getText() == null) {
            throw new GeminiNano.Failure(0, "Gemini Nano gave no answer");
        }
        return cs.get(0).getText().trim();
    }

    private static <T> T await(ListenableFuture<T> f, long seconds) throws GeminiNano.Failure {
        try {
            return f.get(seconds, TimeUnit.SECONDS);
        } catch (ExecutionException e) {
            Throwable c = e.getCause();
            if (c instanceof GenAiException) {
                throw new GeminiNano.Failure(((GenAiException) c).getErrorCode(), c.getMessage());
            }
            throw new GeminiNano.Failure(0, String.valueOf(c));
        } catch (TimeoutException e) {
            f.cancel(true);
            throw new GeminiNano.Failure(0, "Gemini Nano took too long");
        } catch (InterruptedException e) {
            f.cancel(true);
            Thread.currentThread().interrupt();
            throw new GeminiNano.Failure(0, "interrupted");
        }
    }
}
