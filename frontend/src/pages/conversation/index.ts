import "../../app.css";
import { mount } from "svelte";
import ConversationPage from "./ConversationPage.svelte";

const app = mount(ConversationPage, {
  target: document.getElementById("app-root")!,
});

export default app;
