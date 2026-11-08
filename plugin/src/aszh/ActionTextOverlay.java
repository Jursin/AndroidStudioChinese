package aszh;

import com.intellij.ide.ApplicationInitializedListener;
import com.intellij.openapi.actionSystem.ActionManager;
import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.Presentation;
import com.intellij.openapi.application.Application;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.application.ModalityState;
import com.intellij.openapi.diagnostic.Logger;

import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.Properties;

/**
 * Relabels registered actions by id at startup.
 *
 * Android Studio declares most menu texts inline in META-INF/*.xml or hardcodes
 * them in AnAction constructors, so no ResourceBundle can reach them. The
 * action ids, however, are stable - overriding the template presentation works
 * for bundle-backed, descriptor-backed and hardcoded actions alike.
 *
 * Data: /actions.properties, "actionId=中文" plus optional "actionId.description=...".
 */
public final class ActionTextOverlay implements ApplicationInitializedListener {
  private static final Logger LOG = Logger.getInstance(ActionTextOverlay.class);

  @Override
  public void componentsInitialized() {
    Application app = ApplicationManager.getApplication();
    if (app == null) {
      return;
    }
    app.invokeLater(ActionTextOverlay::applyAll, ModalityState.any());
  }

  /**
   * The platform's loader invokes the suspend {@code execute}; the interface default throws
   * PluginException("Override `execute`") when a plugin only implements componentsInitialized().
   */
  @Override
  public Object execute(kotlin.coroutines.Continuation<? super kotlin.Unit> continuation) {
    Application app = ApplicationManager.getApplication();
    if (app != null) {
      app.invokeLater(ActionTextOverlay::applyAll, ModalityState.any());
    }
    return kotlin.Unit.INSTANCE;
  }

  private static void applyAll() {
    try {
      apply();
    }
    catch (Throwable t) {
      LOG.warn("AndroidStudioChinese: action overlay failed", t);
    }
  }

  private static void apply() {
    Properties map = new Properties();
    try (InputStream in = ActionTextOverlay.class.getResourceAsStream("/actions.properties")) {
      if (in == null) {
        LOG.warn("AndroidStudioChinese: actions.properties is missing");
        return;
      }
      map.load(new InputStreamReader(in, StandardCharsets.UTF_8));
    }
    catch (Throwable t) {
      LOG.warn("AndroidStudioChinese: cannot read actions.properties", t);
      return;
    }

    ActionManager manager = ActionManager.getInstance();
    int applied = 0;
    for (String id : map.stringPropertyNames()) {
      if (id.endsWith(".description")) {
        continue;
      }
      String text = map.getProperty(id, "").trim();
      if (text.isEmpty()) {
        continue;
      }
      try {
        AnAction action = manager.getAction(id);
        if (action == null) {
          continue;
        }
        Presentation presentation = action.getTemplatePresentation();
        presentation.setText(text);
        String description = map.getProperty(id + ".description", "").trim();
        if (!description.isEmpty()) {
          presentation.setDescription(description);
        }
        applied++;
      }
      catch (Throwable t) {
        LOG.info("AndroidStudioChinese: skipped action " + id, t);
      }
    }
    LOG.info("AndroidStudioChinese: relabeled " + applied + " actions");
  }
}
