(() => {
  "use strict";
  // Workspace Quick Command forms and menu own their DOM and pending states.
  const tr = window.TermroomT || ((key) => key);

  const workspaceRunMenus = [
    ...document.querySelectorAll("[data-workspace-run-menu]"),
  ];
  const closeWorkspaceRunMenu = (menu, { restoreFocus = false } = {}) => {
    if (!(menu instanceof HTMLDetailsElement) || !menu.open) return false;
    menu.open = false;
    if (restoreFocus) menu.querySelector("summary")?.focus({ preventScroll: true });
    return true;
  };
  workspaceRunMenus.forEach((menu) => {
    menu.querySelector("[data-workspace-run-close]")?.addEventListener("click", (event) => {
      event.stopPropagation();
      closeWorkspaceRunMenu(menu, { restoreFocus: true });
    });

    const addCommand = menu.querySelector("[data-workspace-run-command-add]");
    const commandCards = [
      ...menu.querySelectorAll("[data-workspace-run-command-card]"),
    ];
    const persistedCommandCards = commandCards.filter(
      (card) => card.dataset.workspaceRunCommandPersisted === "true",
    );
    const emptyCommandCards = commandCards.filter(
      (card) => card.dataset.workspaceRunCommandPersisted !== "true",
    );
    const syncCommandCard = (card) => {
      const input = card.querySelector('input[name="command"]');
      const nameInput = card.querySelector('input[name="name"]');
      if (!(input instanceof HTMLInputElement) || !(nameInput instanceof HTMLInputElement)) return;
      const current = input.value.trim();
      const currentName = nameInput.value.trim();
      const dirty = current !== input.defaultValue.trim()
        || currentName !== nameInput.defaultValue.trim();
      const summary = card.querySelector("[data-workspace-command-summary]");
      if (summary) summary.textContent = currentName || summary.dataset.emptyLabel || "";
      card.classList.toggle("is-dirty", dirty);
      const save = card.querySelector("[data-workspace-run-command-save]");
      if (save instanceof HTMLButtonElement) save.disabled = !dirty || !current || !currentName;
      const run = card.querySelector("[data-workspace-command-run-button]");
      if (run instanceof HTMLButtonElement) {
        if (run.dataset.idleTitle === undefined) run.dataset.idleTitle = run.title;
        run.disabled = dirty;
        run.title = dirty
          ? run.dataset.saveFirstTitle || run.dataset.idleTitle
          : run.dataset.idleTitle;
      }
    };
    const syncAddCommand = () => {
      if (addCommand) addCommand.hidden = !commandCards.some((card) => card.hidden);
    };
    const resetEmptyCommandCard = (card) => {
      card.hidden = true;
      const input = card.querySelector('input[name="command"]');
      const nameInput = card.querySelector('input[name="name"]');
      if (input) input.value = "";
      if (nameInput) nameInput.value = "";
      const editor = card.querySelector(".workspace-run-command-editor");
      if (editor instanceof HTMLDetailsElement) editor.open = false;
      syncCommandCard(card);
    };
    const compactEmptyCommandCards = (removedCard) => {
      const remaining = emptyCommandCards
        .filter((card) => !card.hidden && card !== removedCard)
        .map((card) => ({
          value: card.querySelector('input[name="command"]')?.value || "",
          name: card.querySelector('input[name="name"]')?.value || "",
          open: card.querySelector(".workspace-run-command-editor")?.open === true,
        }));
      emptyCommandCards.forEach(resetEmptyCommandCard);
      const visibleCount = Math.max(persistedCommandCards.length ? 0 : 1, remaining.length);
      for (let index = 0; index < visibleCount; index += 1) {
        const card = emptyCommandCards[index];
        if (!card) break;
        card.hidden = false;
        const state = remaining[index];
        const input = card.querySelector('input[name="command"]');
        const nameInput = card.querySelector('input[name="name"]');
        if (input && state) input.value = state.value;
        if (nameInput && state) nameInput.value = state.name;
        const editor = card.querySelector(".workspace-run-command-editor");
        if (editor instanceof HTMLDetailsElement) editor.open = Boolean(state?.open);
        syncCommandCard(card);
      }
      syncAddCommand();
      const visibleCards = commandCards.filter((card) => !card.hidden);
      visibleCards.at(-1)?.querySelector("summary")?.focus({ preventScroll: true });
    };
    addCommand?.addEventListener("click", () => {
      const card = commandCards.find((candidate) => candidate.hidden);
      if (!card) return;
      card.hidden = false;
      card.querySelector("summary")?.focus({ preventScroll: true });
      syncAddCommand();
    });
    commandCards.forEach((card) => {
      const input = card.querySelector('input[name="command"]');
      const nameInput = card.querySelector('input[name="name"]');
      const editor = card.querySelector(".workspace-run-command-editor");
      const cancel = card.querySelector("[data-workspace-run-command-cancel]");
      input?.addEventListener("input", () => syncCommandCard(card));
      nameInput?.addEventListener("input", () => syncCommandCard(card));
      cancel?.addEventListener("click", () => {
        if (input instanceof HTMLInputElement) input.value = input.defaultValue;
        if (nameInput instanceof HTMLInputElement) nameInput.value = nameInput.defaultValue;
        syncCommandCard(card);
        if (editor instanceof HTMLDetailsElement) editor.open = false;
        card.querySelector("summary")?.focus({ preventScroll: true });
      });
      editor?.addEventListener("toggle", () => {
        if (!(editor instanceof HTMLDetailsElement) || !editor.open) return;
        window.requestAnimationFrame(() => nameInput?.focus({ preventScroll: true }));
      });
      syncCommandCard(card);
    });
    emptyCommandCards.forEach((card) => {
      card.querySelector("[data-workspace-run-command-remove]")?.addEventListener(
        "click",
        () => compactEmptyCommandCards(card),
      );
    });
    syncAddCommand();
  });
  document.addEventListener("click", (event) => {
    workspaceRunMenus
      .filter((menu) => menu.open && !menu.contains(event.target))
      .forEach((menu) => closeWorkspaceRunMenu(menu));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const menu = workspaceRunMenus.find((candidate) => candidate.open);
    if (!menu) return;
    event.preventDefault();
    closeWorkspaceRunMenu(menu, { restoreFocus: true });
  });

  const workspaceCommandForms = [
    ...document.querySelectorAll("[data-workspace-command-run]"),
  ];
  const workspaceCommandAnnouncer = document.querySelector(
    "[data-workspace-command-announcer]",
  );
  const resetWorkspaceCommandPending = (form) => {
    form.removeAttribute("aria-busy");
    delete form.dataset.workspaceCommandStarting;
    const button = form.querySelector('button[type="submit"]');
    if (button) {
      button.removeAttribute("aria-busy");
      button.removeAttribute("aria-disabled");
    }
    const label = form.querySelector("[data-workspace-command-label]");
    if (label?.dataset.workspaceCommandIdleLabel !== undefined) {
      label.textContent = label.dataset.workspaceCommandIdleLabel;
    }
  };
  workspaceCommandForms.forEach((form) => {
    const label = form.querySelector("[data-workspace-command-label]");
    if (label) label.dataset.workspaceCommandIdleLabel = label.textContent || "";
    form.addEventListener("submit", (event) => {
      if (form.dataset.workspaceCommandStarting === "true") {
        event.preventDefault();
        return;
      }
      const button = form.querySelector('button[type="submit"]');
      const startingLabel = form.dataset.workspaceCommandStartingLabel
        || tr("workspace.run.starting");
      form.dataset.workspaceCommandStarting = "true";
      form.setAttribute("aria-busy", "true");
      if (button) {
        button.setAttribute("aria-busy", "true");
        button.setAttribute("aria-disabled", "true");
      }
      if (label) label.textContent = startingLabel;
      if (workspaceCommandAnnouncer) {
        workspaceCommandAnnouncer.textContent = startingLabel;
      }
    });
  });
  window.addEventListener("pageshow", () => {
    workspaceCommandForms.forEach(resetWorkspaceCommandPending);
    if (workspaceCommandAnnouncer) workspaceCommandAnnouncer.textContent = "";
  });

})();
