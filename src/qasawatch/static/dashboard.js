(() => {
  const provider = document.querySelector("#email-provider");
  const gmailHelp = document.querySelector("#gmail-email-help");
  const customSettings = document.querySelector("#custom-email-settings");
  const updateEmailProvider = () => {
    if (!provider || !gmailHelp || !customSettings) {
      return;
    }
    const gmail = provider.value === "gmail";
    gmailHelp.hidden = !gmail;
    customSettings.hidden = gmail;
  };
  provider?.addEventListener("change", updateEmailProvider);
  updateEmailProvider();

  const settingsForm = document.querySelector(".settings-form");
  const destinationList = document.querySelector("#commute-destinations");
  const destinationTemplate = document.querySelector("#commute-destination-template");
  const destinationJson = document.querySelector("#destinations-json");
  const addDestination = document.querySelector("#add-commute-destination");

  const renumberDestinations = () => {
    destinationList?.querySelectorAll(".destination-card").forEach((card, index) => {
      const title = card.querySelector(".destination-title");
      if (title) {
        title.textContent = `Destination ${index + 1}`;
      }
    });
  };

  const serializeDestinations = () => {
    if (!destinationList || !destinationJson) {
      return;
    }
    const destinations = [...destinationList.querySelectorAll(".destination-card")]
      .map((card) => {
        const value = (name) =>
          card.querySelector(`[data-destination-field="${name}"]`)?.value.trim() || "";
        const maximum = value("maximum_commute_minutes");
        return {
          label: value("label"),
          address: value("address"),
          commute_mode: value("commute_mode") || "arrival",
          maximum_commute_minutes: maximum ? Number(maximum) : null,
        };
      })
      .filter((item) => item.label || item.address || item.maximum_commute_minutes);
    destinationJson.value = JSON.stringify(destinations);
  };

  const bindDestinationCard = (card) => {
    card.querySelector(".remove-destination")?.addEventListener("click", () => {
      card.remove();
      renumberDestinations();
      serializeDestinations();
    });
    card.querySelectorAll("input, select").forEach((control) => {
      control.addEventListener("input", serializeDestinations);
      control.addEventListener("change", serializeDestinations);
    });
  };

  destinationList?.querySelectorAll(".destination-card").forEach(bindDestinationCard);
  addDestination?.addEventListener("click", () => {
    if (!destinationList || !destinationTemplate) {
      return;
    }
    const card = destinationTemplate.content.firstElementChild.cloneNode(true);
    destinationList.append(card);
    bindDestinationCard(card);
    renumberDestinations();
    serializeDestinations();
    card.querySelector("input")?.focus();
  });
  settingsForm?.addEventListener("submit", serializeDestinations);
  renumberDestinations();
  serializeDestinations();

  const liveSelectors = [
    "#live-monitoring-status",
    "#live-next-check",
    "#live-system-details",
    "#live-connection-maps",
    "#live-connection-discord",
    "#live-connection-sheets",
    "#live-connection-email",
  ];
  let liveRefreshInProgress = false;

  const copyLiveElement = (freshDocument, selector) => {
    const current = document.querySelector(selector);
    const fresh = freshDocument.querySelector(selector);
    if (!current || !fresh) {
      return;
    }
    current.className = fresh.className;
    current.innerHTML = fresh.innerHTML;
  };

  const bindShowOlder = (root = document) => {
    root.querySelectorAll("[data-show-older]").forEach((button) => {
      if (button.dataset.bound === "true") {
        return;
      }
      button.dataset.bound = "true";
      button.addEventListener("click", () => {
        const section = button.closest("details");
        const olderItems = section?.querySelectorAll("[data-older-item]") || [];
        const showing = [...olderItems].some((item) => item.hidden);
        olderItems.forEach((item) => {
          item.hidden = !showing;
        });
        const baseLabel = button.textContent.replace(
          /^(Show older|Show fewer)/,
          "",
        ).trim();
        button.textContent = `${showing ? "Show fewer" : "Show older"} ${baseLabel}`;
      });
    });
  };

  bindShowOlder();

  const refreshLiveDashboard = async () => {
    if (document.hidden || liveRefreshInProgress) {
      return;
    }
    liveRefreshInProgress = true;
    try {
      const response = await fetch("/", {
        headers: { Accept: "text/html", "X-QasaWatch-Live": "1" },
        cache: "no-store",
      });
      if (!response.ok) {
        return;
      }
      const freshDocument = new DOMParser().parseFromString(
        await response.text(),
        "text/html",
      );
      liveSelectors.forEach((selector) => copyLiveElement(freshDocument, selector));

      const currentActivity = document.querySelector("#live-activity");
      const freshActivity = freshDocument.querySelector("#live-activity");
      if (
        currentActivity &&
        freshActivity &&
        currentActivity.dataset.activityVersion !==
          freshActivity.dataset.activityVersion
      ) {
        currentActivity.replaceWith(freshActivity);
        bindShowOlder(freshActivity);
      }
    } catch (_error) {
      // A temporary dashboard/network failure should not interrupt editing.
    } finally {
      liveRefreshInProgress = false;
    }
  };

  window.setInterval(refreshLiveDashboard, 15_000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      refreshLiveDashboard();
    }
  });

  const form = document.querySelector("#run-now-form");
  const button = document.querySelector("#run-now-button");
  const dialog = document.querySelector("#run-result-dialog");

  if (!form || !button || !dialog) {
    return;
  }

  const title = dialog.querySelector("#run-result-title");
  const message = dialog.querySelector("#run-result-message");
  const counts = dialog.querySelector("#run-result-counts");
  const refreshButton = dialog.querySelector("#refresh-after-run");
  const fields = {
    found: dialog.querySelector("#run-result-found"),
    total_available: dialog.querySelector("#run-result-total-available"),
    pages_scanned: dialog.querySelector("#run-result-pages-scanned"),
    new: dialog.querySelector("#run-result-new"),
    accepted: dialog.querySelector("#run-result-accepted"),
    rejected: dialog.querySelector("#run-result-rejected"),
  };

  const openDialog = () => {
    document.body.classList.add("dialog-open");
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  };

  const closeDialog = () => {
    document.body.classList.remove("dialog-open");
    if (typeof dialog.close === "function") {
      dialog.close();
    } else {
      dialog.removeAttribute("open");
    }
  };

  dialog.querySelectorAll("[data-close-dialog]").forEach((control) => {
    control.addEventListener("click", closeDialog);
  });
  dialog.addEventListener("cancel", () => {
    document.body.classList.remove("dialog-open");
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      closeDialog();
    }
  });
  refreshButton.addEventListener("click", () => window.location.reload());

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    button.textContent = "Checking…";
    title.textContent = "Checking Qasa…";
    message.textContent = "This can take a moment while the saved search is loaded.";
    message.classList.remove("is-error");
    counts.hidden = true;
    refreshButton.hidden = true;
    openDialog();

    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      let result = {};
      try {
        result = await response.json();
      } catch (_error) {
        // The friendly fallback below is more useful than a JSON parsing error.
      }

      if (!response.ok) {
        throw new Error(result.detail || result.message || "The check could not be completed.");
      }

      title.textContent = "Check complete";
      if (result.truncated) {
        message.textContent =
          `Checked ${Number(result.found || 0).toLocaleString()} of ` +
          `${Number(result.total_available || 0).toLocaleString()} Qasa listings ` +
          `across ${Number(result.pages_scanned || 0).toLocaleString()} pages. ` +
          "Increase the pagination limits in Search settings to inspect more.";
      } else {
        message.textContent =
          Number(result.new || 0) > 0
            ? "New listings were found. Refresh the activity section to see the latest details."
            : "The saved search was checked successfully. No new listings were found.";
      }
      Object.entries(fields).forEach(([name, element]) => {
        element.textContent = Number(result[name] || 0).toLocaleString();
      });
      counts.hidden = false;
      refreshButton.hidden = false;
    } catch (error) {
      title.textContent = "Check unsuccessful";
      message.textContent =
        error instanceof Error ? error.message : "The check could not be completed.";
      message.classList.add("is-error");
    } finally {
      button.disabled = false;
      button.textContent = "Check now";
    }
  });
})();
