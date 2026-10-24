import { test, expect, finishLogin } from "./fixture.ts";

function previewPdf(): Buffer {
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    "<< /Length 50 >>\nstream\nBT /F1 12 Tf 20 100 Td (Authorized preview) Tj ET\nendstream",
  ];
  let pdf = "%PDF-1.4\n";
  const offsets = [0];
  objects.forEach((object,index) => {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index+1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(offset => String(offset).padStart(10,"0")+" 00000 n ").join("\n")}\ntrailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf);
}

test("Pengolo selects its default model when the catalogue precedes the composer", async ({page}) => {
  let workspaces = 0;
  await page.routeWebSocket("**/graphql", socket => socket.onMessage(raw => {
    const frame = JSON.parse(String(raw)) as {type:string};
    if (frame.type === "connection_init") socket.send(JSON.stringify({type:"connection_ack"}));
  }));
  await page.route("**/graphql", async route => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataJSON() as {query:string};
    if (!body.query.includes("query Workspace")) return route.continue();
    workspaces++;
    await route.fulfill({json:{data:{
      pengoloIdentity:{version:"ready-models",webSearchAvailable:true,administrator:false},
      pengoloModels:{defaultModel:"fixture",models:[{alias:"fixture",model:"e2e",provider:"fixture",reasoningEfforts:["medium"]}]},
      pengoloProjects:[],pengoloChats:{items:[],after:null},
    }}});
  });
  await finishLogin(page,"browser-pengolo-models");
  await expect.poll(() => workspaces).toBe(1);
  await page.getByRole("button",{name:"Pengolo",exact:true}).click();
  const composer = page.getByRole("complementary",{name:"Pengolo assistant",exact:true});
  await expect(composer.getByRole("combobox",{name:"Model",exact:true})).toHaveText("e2e");
  await composer.getByRole("button",{name:"Open Pengolo workspace",exact:true}).click();
  await expect(page).toHaveURL(/\/pengolo$/);
  await expect(composer).toHaveCount(0);
  await expect(page.getByRole("combobox",{name:"Model",exact:true})).toHaveText("e2e");
  await page.reload();
  await expect(page.getByRole("combobox",{name:"Model",exact:true})).toHaveText("e2e");
  await page.getByRole("button",{name:"Add files and tools",exact:true}).click();
  await page.getByRole("menuitem",{name:"Web search",exact:true}).click();
  await expect(page.locator(".p-composer")).toContainText("Web search");
  await expect(page.getByPlaceholder("Enter only information that may be sent publicly")).toHaveCount(0);
  await page.getByLabel("Message Pengolo",{exact:true}).fill("Find current university examination rules");
  await expect(page.getByRole("button",{name:"Send message",exact:true})).toBeEnabled();
});

test("Pengolo drafts stay local and dynamic models survive reload without inference", async ({page}) => {
  let sends = 0;
  let oauthStarted = false;
  let openaiConnected = false;
  const openaiId = "9".repeat(32);
  const openaiStatus = () => ({enabled:true,connected:openaiConnected,quotaExhausted:false,selected:openaiConnected ? openaiId : null,usageUrl:"https://chatgpt.com/settings/usage",authorizationUrl:null,revocationConfirmed:null,accounts:openaiConnected ? [{id:openaiId,label:"Plan account",connected:true}] : []});
  await page.context().route("https://auth.openai.com/**", route => route.fulfill({contentType:"text/html",body:"OAuth fixture"}));
  let identityVersion = 1;
  let libraryProgress = 65;
  const workspaceRequests: string[] = [];
  const chatRequests: string[] = [];
  const identityChannel = {push: () => {}};
  let releaseIdentity: () => void = () => {};
  const initialIdentity = new Promise<void>((resolve) => { releaseIdentity = resolve; });
  await page.addInitScript(() => {
    window.addEventListener("pengolo-identity-verified", () => {
      document.documentElement.dataset.pengoloIdentityVerified = "true";
    });
  });
  const chat = {id:"a".repeat(32),ownerId:"browser-pengolo",projectId:null,revision:1,updatedAt:1,expiresAt:null,data:{title:"Business question",pinned:false,activeGeneration:null,messages:[{id:"user",role:"user",text:"Business question"},{id:"answer",role:"assistant",text:"Business result\n\n## Une vue claire de votre investigation\n\nLes documents et le contexte du graphe permettent de relier les opérations à leurs acteurs. Chaque source reste soumise à vos permissions Galadril.\n\n| Élément | Contexte disponible |\n| :--- | :--- |\n| Transactions | Relations et événements associés |\n| Documents | Bibliothèque de ce projet |\n| Sources | Références vérifiables |\n\n### Prochaine étape\n\nSuivez les relations pertinentes pour préciser votre analyse, puis comparez les éléments de preuve.\n\n```python\nsummary = sum([120, 80, 250])\n```"}]}};
  const header = {...chat,data:{title:chat.data.title,pinned:false,activeGeneration:null}};
  const chatPage = {...chat,data:{...chat.data,messages:{items:chat.data.messages,after:null}}};
  const identity = () => ({version:`browser-pengolo-${identityVersion}`,webSearchAvailable:true,administrator:false});
  const projects = [{id:"c".repeat(32),ownerId:"browser-pengolo",projectId:null,revision:1,updatedAt:1,expiresAt:null,data:{name:"Veille opérationnelle",icon:"🧭",color:"#d97706",shared:false,roles:[] as string[],contextMode:"project"}}];
  projects.push({id:"e".repeat(32),ownerId:"browser-pengolo",projectId:null,revision:1,updatedAt:1,expiresAt:null,data:{name:"Shared investigation",icon:"📁",color:"#d97706",shared:true,roles:["staff","lead"],contextMode:"project"}});
  const models = {defaultModel:"team-model",models:[{alias:"team-model",model:"Configured team model",provider:"configured",reasoningEfforts:["medium"]},{alias:"unsupported",model:"Unsupported",provider:"configured",reasoningEfforts:[]}]};
  await page.routeWebSocket("**/graphql", socket => {
    socket.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as {type:string;id:string;payload?:{query:string}};
      if (frame.type === "connection_init") socket.send(JSON.stringify({type:"connection_ack"}));
      if (frame.type !== "subscribe") return;
      if (frame.payload?.query.includes("pengoloIdentityChanges")) {
        identityChannel.push = () => socket.send(JSON.stringify({type:"next",id:frame.id,payload:{data:{pengoloIdentityChanges:identity()}}}));
        identityChannel.push();
      } else if (frame.payload?.query.includes("pengoloStream")) {
        socket.send(JSON.stringify({type:"next",id:frame.id,payload:{data:{pengoloStream:{generationId:frame.id,status:"running",events:[{sequence:1,kind:"content",content:chat.data.messages[1]?.text}],message:null,chatRevision:null,chatUpdatedAt:null}}}}));
        socket.send(JSON.stringify({type:"next",id:frame.id,payload:{data:{pengoloStream:{generationId:frame.id,status:"completed",events:[{sequence:2,kind:"completed",content:""}],message:chat.data.messages[1],chatRevision:2,chatUpdatedAt:2}}}}));
      }
    });
  });
  await page.route("**/graphql", async (route) => {
    const request = route.request();
    if (request.method() !== "POST") return route.continue();
    const body: unknown = request.postDataJSON();
    if (typeof body !== "object" || body === null || !("query" in body) || typeof body.query !== "string") return route.continue();
    const query = body.query;
    if (/pengolo(?:Models|Chats|Projects)/.test(query)) workspaceRequests.push(query);
    if (query.includes("pengoloChat(") && !query.includes("query Workspace")) chatRequests.push(query);
    let data: Record<string, unknown>;
    if (query.includes("query Workspace")) {
      await initialIdentity;
      const variables = (body as {variables?:{withChat?:boolean}}).variables;
      data = {pengoloIdentity:identity(),pengoloModels:models,pengoloProjects:projects,pengoloChats:{items:sends ? [header] : [],after:null},...(variables?.withChat ? {activeChat:chatPage} : {})};
    }
    else if (query.includes("updatePengoloOpenai")) {
      const operation = (body as {variables?:{input:{operation:string}}}).variables?.input.operation;
      if (operation === "CONNECT") {oauthStarted = true;data = {updatePengoloOpenai:{...openaiStatus(),authorizationUrl:"https://auth.openai.com/api/accounts/authorize?response_type=code"}};}
      else {openaiConnected = false;data = {updatePengoloOpenai:{...openaiStatus(),revocationConfirmed:true}};}
    }
    else if (query.includes("pengoloOpenai")) {if (oauthStarted) {openaiConnected = true;oauthStarted = false;}data = {pengoloOpenai:openaiStatus()};}
    else if (query.includes("pengoloModels")) data = {pengoloModels:openaiConnected ? {...models,models:[...models.models,{alias:"plan-model",model:"Plan model",provider:"openai",reasoningEfforts:["medium"]}]} : models};
    else if (query.includes("pengoloLibrary")) data = {pengoloLibrary:{items:identityVersion === 1 ? [{id:"d".repeat(32),ownerId:"browser-pengolo",projectId:null,revision:1,updatedAt:1,expiresAt:Date.now()+86400000,data:{name:"Cached evidence.pdf",mime:"application/pdf",size:12,status:"ready",temporarySession:null,indexing:libraryProgress < 100 ? {stage:"embedding",percent:libraryProgress} : null,retrieval:{status:"ready",profileId:null,warnings:[]}}}] : [],after:null}};
    else if (query.includes("pengoloDownload")) data = {pengoloDownload:{url:"/fixture-preview.pdf"}};
    else if (query.includes("pengoloChats")) data = {pengoloChats:{items:sends ? [header] : [],after:null}};
    else if (query.includes("sendPengolo")) { sends++; data = {sendPengolo:{chat:{...header,data:{...header.data,activeGeneration:"b".repeat(32)}},generation:{id:"b".repeat(32),data:{status:"completed"}},userMessage:chat.data.messages[0]}}; }
    else if (query.includes("savePengoloProject")) {
      const input = (body as {variables?:{input:{id:string;revision:number;name:string;roles:string[]}}}).variables?.input;
      if (!input) throw new Error("Project input missing");
      expect(input.id).toBe("e".repeat(32));
      expect(input.revision).toBe(1);
      expect(input.roles).toEqual(["staff","lead","operations"]);
      const saved = {...projects[1]!,revision:2,data:{...projects[1]!.data,name:input.name,roles:input.roles}};
      projects[1] = saved;
      data = {savePengoloProject:saved};
    }
    else if (query.includes("pengoloChat(")) data = {pengoloChat:chatPage};
    else return route.continue();
    await route.fulfill({json:{data}});
  });
  await finishLogin(page, "browser-pengolo");
  workspaceRequests.length = 0;
  await page.goto("/pengolo");
  await expect(page.getByRole("heading",{name:"How can Pengolo help?"})).toBeVisible();
  await page.getByLabel("Message Pengolo",{exact:true}).fill("Draft before permission verification");
  releaseIdentity();
  await expect(page.locator("html")).toHaveAttribute("data-pengolo-identity-verified", "true", {timeout:25_000});
  expect(workspaceRequests).toHaveLength(1);
  await expect(page.getByLabel("Message Pengolo",{exact:true})).toHaveValue("Draft before permission verification");
  await page.getByLabel("Account menu",{exact:true}).click();
  await page.getByRole("link",{name:"User settings",exact:true}).click();
  await expect(page.getByRole("heading",{name:"User settings",exact:true})).toBeVisible();
  await expect(page.getByText(/Without a connected account/)).toBeVisible();
  const planSettings = page.getByRole("region",{name:"OpenAI connection",exact:true});
  await expect(planSettings).toContainText("Codex / ChatGPT Work");
  const popupEvent = page.waitForEvent("popup");
  await planSettings.getByRole("button",{name:"Continue with ChatGPT",exact:true}).click();
  const popup = await popupEvent;
  await expect(popup).toHaveURL(/https:\/\/auth\.openai\.com\/api\/accounts\/authorize/);
  await expect(planSettings).toContainText("Plan account",{timeout:10_000});
  await expect(planSettings.getByRole("link",{name:"Manage usage",exact:true})).toHaveAttribute("href","https://chatgpt.com/settings/usage");
  await page.screenshot({path:"/tmp/results/pengolo-chatgpt-account.png",fullPage:true});
  await planSettings.getByRole("button",{name:"Disconnect Plan account",exact:true}).click();
  await expect(planSettings.getByText("Plan account",{exact:true})).toHaveCount(0);
  await popup.close();
  await page.goto("/pengolo");

  await expect(page.getByRole("button",{name:"New chat",exact:true}).locator("svg")).toHaveCount(1);
  for (let index=0;index<5;index++) await page.getByRole("button",{name:"New chat",exact:true}).click();
  expect(sends).toBe(0);
  expect(await page.evaluate(() => localStorage.getItem("pengolo_chat_sessions"))).toBeNull();
  await expect(page.getByRole("combobox",{name:"Model",exact:true})).toHaveText("Configured team model");
  await page.getByRole("combobox",{name:"Model",exact:true}).click();
  await expect(page.getByRole("option")).toHaveCount(1);
  await page.keyboard.press("Escape");
  const heading = page.getByRole("heading", {name: "How can Pengolo help?"});
  const composer = page.locator(".p-composer");
  const headingBox = await heading.boundingBox();
  const composerBox = await composer.boundingBox();
  expect(headingBox).not.toBeNull();
  expect(composerBox).not.toBeNull();
  if (headingBox && composerBox) expect(composerBox.y - headingBox.y - headingBox.height).toBeLessThan(64);
  expect(await page.getByLabel("Message Pengolo", {exact:true}).evaluate((element) => Number.parseFloat(getComputedStyle(element).fontSize))).toBeGreaterThanOrEqual(16);
  const temporary = page.getByRole("button", {name:"Temporary chat", exact:true});
  await expect(temporary).toHaveAttribute("aria-pressed", "false");
  await temporary.focus();
  await expect(page.getByRole("tooltip",{name:"Temporary chat",exact:true})).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("checkbox", {name:"Use my personal library", exact:true})).toHaveCount(0);
  await temporary.click();
  await expect(temporary).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("checkbox", {name:"Use my personal library", exact:true})).toBeVisible();
  await page.getByRole("checkbox", {name:"Use my personal library", exact:true}).check();
  await expect(page.locator(".p-temporary-banner")).toContainText("Your personal library is available");
  await temporary.click();
  await expect(page.getByRole("checkbox", {name:"Use my personal library", exact:true})).toHaveCount(0);
  await page.getByRole("button", {name:"Add files and tools", exact:true}).focus();
  await expect(page.getByRole("tooltip", {name:"Add files and tools", exact:true})).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", {name:"Send message", exact:true})).not.toHaveAttribute("title");
  await page.getByRole("button",{name:"Add files and tools",exact:true}).click();
  await page.getByRole("button",{name:"Add files and tools",exact:true}).click();
  await page.setViewportSize({width:390,height:740});
  await expect(page.getByRole("button",{name:"Open Pengolo sidebar",exact:true})).toBeVisible();
  await page.setViewportSize({width:1440,height:1000});
  const sidebarMotion = await page.addStyleTag({content: ".p-sidebar { transition-duration: 1s; }"});
  await page.getByRole("button",{name:"Open Pengolo sidebar",exact:true}).click();
  await expect(page.locator(".p-sidebar")).not.toHaveClass(/closed/);
  const createProject = page.getByRole("button",{name:"Create project",exact:true});
  await createProject.evaluate(element => element.scrollIntoView({block:"nearest",inline:"nearest"}));
  expect(await page.locator(".p-sidebar").evaluate(element => element.scrollLeft)).toBe(0);
  await sidebarMotion.evaluate(element => element.parentNode?.removeChild(element));
  await expect(page.getByRole("button",{name:"Create project",exact:true})).toBeVisible();
  await page.getByRole("button",{name:"Projects",exact:true}).hover();
  await page.getByRole("button",{name:"Create project",exact:true}).click();
  await expect(page.getByRole("dialog",{name:"Create project",exact:true})).toBeVisible();
  const projectDialog = page.getByRole("dialog",{name:"Create project",exact:true});
  await projectDialog.getByLabel("Project name",{exact:true}).fill("Sidebar interaction regression");
  const access = projectDialog.getByRole("combobox",{name:/^Access/});
  await expect(access).toBeVisible();
  await expect(access).toHaveText("Personal · only you");
  await expect(projectDialog.getByRole("textbox",{name:/^Roles/})).toHaveCount(0);
  await access.click();
  await page.getByRole("option",{name:"Shared · selected Galadril roles",exact:true}).click();
  await expect(access).toHaveText("Shared · selected Galadril roles");
  const roles = projectDialog.getByRole("textbox",{name:/^Roles/});
  await roles.fill("pengolo_reader");
  await expect(roles).toHaveValue("pengolo_reader");
  const documentContext = projectDialog.getByRole("combobox",{name:/^Document context/});
  await expect(documentContext).toHaveText("Project library only");
  await expect(documentContext).toBeDisabled();
  await page.getByRole("button",{name:"Customize project appearance",exact:true}).click();
  await page.getByRole("button",{name:"Close dialog",exact:true}).click();
  const sharedProject = page.locator(".p-project").filter({hasText:"Shared investigation"});
  const projectOptions = sharedProject.getByRole("button",{name:"Options for project Shared investigation",exact:true});
  await sharedProject.hover();
  await expect(projectOptions).toBeVisible({timeout:5000});
  await expect(sharedProject.locator(".p-project-children")).toBeHidden();
  await projectOptions.click();
  const projectMenu = page.getByRole("menu",{name:"Options for project Shared investigation",exact:true});
  const editAction = projectMenu.getByRole("menuitem",{name:"Rename and permissions",exact:true});
  await expect.poll(async () => {
    const menuBox = await projectMenu.boundingBox();
    const actionBox = await editAction.boundingBox();
    return !!menuBox && !!actionBox && actionBox.y >= menuBox.y && actionBox.y + actionBox.height <= menuBox.y + menuBox.height;
  }).toBe(true);
  await page.screenshot({path:"/tmp/results/pengolo-project-menu.png",fullPage:true});
  await sharedProject.getByRole("menuitem",{name:"Rename and permissions",exact:true}).click();
  const editProject = page.getByRole("dialog",{name:"Edit project",exact:true});
  await editProject.getByLabel("Project name",{exact:true}).fill("Renamed shared investigation");
  await editProject.getByRole("textbox",{name:/^Roles/}).fill("staff, lead, operations");
  await page.screenshot({path:"/tmp/results/pengolo-project-permissions.png",fullPage:true});
  await editProject.getByRole("button",{name:"Save project",exact:true}).click();
  await expect(editProject).not.toBeVisible();
  await expect(page.locator(".p-project summary").filter({hasText:"Renamed shared investigation"})).toBeVisible();
  const project = page.locator(".p-project").filter({hasText:"Veille opérationnelle"});
  await project.locator("summary").click();
  await project.getByRole("link",{name:"Library for Veille opérationnelle",exact:true}).click();
  await expect(page.getByRole("region",{name:"File library",exact:true}).locator('input[type="file"]')).toHaveCount(1);
  await expect(page.getByRole("combobox",{name:"Choose library",exact:true})).toHaveText("Veille opérationnelle");
  await page.getByRole("button",{name:"New chat",exact:true}).click();
  await page.getByLabel("Message Pengolo",{exact:true}).fill("Business question");
  await page.getByRole("button",{name:"Send message",exact:true}).click();
  await expect(page.getByText("Business result",{exact:true})).toBeVisible();
  await expect(page.getByRole("button",{name:"Stop generation",exact:true})).not.toBeVisible();
  expect(chatRequests).toHaveLength(0);
  await expect(page).toHaveURL(/\/pengolo\/[a-f0-9]{32}$/);
  await page.reload();
  await expect(page.getByText("Business result",{exact:true})).toBeVisible();
  expect(sends).toBe(1);
  await expect(page.locator(".p-notice")).toHaveCount(0);
  await page.setViewportSize({width:390,height:740});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.route("**/fixture-preview.pdf*", (route) => route.fulfill({body:previewPdf(),contentType:"application/pdf",headers:{"content-disposition":"inline; filename=preview.pdf"}}));
  await page.goto("/pengolo/library");
  await expect(page.locator("html")).toHaveAttribute("data-pengolo-identity-verified", "true", {timeout:25_000});
  const indexProgress = page.getByRole("progressbar", {name:"Indexing progress for Cached evidence.pdf",exact:true});
  await expect(indexProgress).toHaveAttribute("value", "65");
  await expect(page.getByText("Computing embeddings · 65%",{exact:false})).toBeVisible();
  libraryProgress = 100;
  await expect(indexProgress).toHaveCount(0, {timeout:10_000});
  await expect(page.getByText("Document indexed",{exact:true})).toBeVisible();
  await page.screenshot({path:"/tmp/results/pengolo-library-progress.png",fullPage:true});
  await page.getByRole("button", {name:"Actions for Cached evidence.pdf", exact:true}).click();
  await page.getByRole("menuitem", {name:"Preview", exact:true}).click();
  await expect(page.getByRole("dialog", {name:"Cached evidence.pdf", exact:true})).toBeVisible();
  await expect(page.getByRole("dialog", {name:"Cached evidence.pdf", exact:true}).locator("iframe")).not.toHaveAttribute("sandbox");
  identityVersion++;
  identityChannel.push();
  await expect(page.getByRole("dialog", {name:"Cached evidence.pdf", exact:true})).toHaveCount(0, {timeout:25_000});
  await page.getByRole("button",{name:"Open Pengolo sidebar",exact:true}).click();
  await page.getByRole("link",{name:"Scheduled",exact:true}).click();
  await expect(page.getByRole("heading",{name:"Coming soon",exact:true})).toBeVisible();
});

test("Pengolo pages ten messages, preserves attachments and persists rich Markdown tasks", async ({page,context}) => {
  const id = "f".repeat(32);
  let authorized = true;
  const storedChatScopes: boolean[] = [];
  let checked = false;
  let revision = 1;
  const requests: Array<{query:string;variables:Record<string,unknown>}> = [];
  const remoteImages: string[] = [];
  page.on("request", request => { if (new URL(request.url()).hostname === "images.example.invalid") remoteImages.push(request.url()); });
  const rich = "[Sommaire](#résumé) [Source](https://example.com/source)\n\n## Résumé\n\n- [ ] Vérifier la source\n\nUne note[^preuve] et $E=mc^2$.\n\n```python\ndef answer():\n    return 42\n```\n\n| Label | Value |\n| --- | --- |\n| A | 42 |\n\n```chart\n{\"type\":\"donut\",\"title\":\"Répartition\",\"labels\":[\"A\",\"B\"],\"series\":[{\"name\":\"Parts\",\"values\":[40,60]}]}\n```\n\n![Photo](https://images.example.invalid/photo.png)\n\n[^preuve]: Source vérifiée.";
  const messages = () => Array.from({length:30}, (_,index) => ({id:`m${index}`,role:"assistant",status:"completed",text:index === 29 ? rich : `History ${index}\n\n${"Evidence details. ".repeat(20)}`,fileIds:index === 10 ? ["e".repeat(32)] : [],taskChecks:index === 29 ? [{index:0,checked}] : []}));
  const header = () => ({id,ownerId:"browser-pengolo-history",projectId:null,expiresAt:null,revision,updatedAt:revision,data:{title:"History",pinned:false,activeGeneration:null}});
  const paged = (before = 30) => ({items:messages().slice(Math.max(0,before-10),before),after:before > 10 ? `m${before-10}` : null});
  await page.routeWebSocket("**/graphql", socket => socket.onMessage(raw => {
    const frame = JSON.parse(String(raw)) as {type:string;id:string};
    if (frame.type === "connection_init") socket.send(JSON.stringify({type:"connection_ack"}));
  }));
  await page.route("**/media/image?**", route => route.fulfill({contentType:"image/png",body:Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFQAAAABJRU5ErkJggg==","base64")}));
  await page.route("**/graphql", async route => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataJSON() as {query:string;variables:Record<string,unknown>};
    const query = body.query;
    let data: Record<string,unknown>;
    if (query.includes("query Workspace")) {
      requests.push(body);
      data = {pengoloIdentity:{version:authorized ? "history" : "history-revoked",webSearchAvailable:true,administrator:false},pengoloModels:{defaultModel:"fixture",models:[{alias:"fixture",model:"Fixture",provider:"fixture",reasoningEfforts:["medium"]}]},pengoloProjects:[],pengoloChats:{items:authorized ? [header()] : [],after:null},...(body.variables.withChat ? {activeChat: authorized ? {...header(),data:{...header().data,messages:paged()}} : null} : {})};
    } else if (query.includes("query OlderMessages")) {
      requests.push(body);
      data = {pengoloChat:{id,data:{messages:paged(Number(String(body.variables.before).slice(1)))}}};
    } else if (query.includes("query Chat(")) {
      storedChatScopes.push(body.variables.temporary === true);
      data = {pengoloChat:{...header(),data:{...header().data,messages:paged()}}};
    } else if (query.includes("updatePengoloChat")) {
      const input = body.variables.input as {taskChecks:Array<{index:number;checked:boolean}>};
      checked = input.taskChecks[0]?.checked === true;
      revision++;
      data = {updatePengoloChat:header()};
    } else return route.continue();
    await route.fulfill({json:{data}});
  });
  await finishLogin(page,"browser-pengolo-history");
  requests.length = 0;
  await page.goto(`/pengolo/${id}`);
  await expect(page.getByRole("article")).toHaveCount(10);
  await expect(page.getByRole("article").last()).toBeInViewport();
  expect(requests).toHaveLength(1);
  expect(requests[0]?.query).toMatch(/messages\(limit: 10\)/);
  await expect(page.locator(".hljs-keyword").first()).toBeVisible();
  await expect(page.locator(".katex-html")).toHaveCount(1);
  expect(await page.locator(".katex-mathml").evaluate(element => getComputedStyle(element).position)).toBe("absolute");
  await expect(page.getByRole("link",{name:"Source",exact:true})).toHaveAttribute("target","_blank");
  await page.getByRole("link",{name:"Sommaire",exact:true}).click();
  await expect(page).toHaveURL(/#p-m29-resume$/);
  await page.locator('[data-tour="search"]').click();
  const cachedSearch = page.getByRole("dialog", {name:"Search Galadril"});
  await cachedSearch.getByRole("textbox").fill("vérifer");
  await expect(cachedSearch.getByText("History",{exact:true})).toBeVisible();
  await cachedSearch.getByText("History",{exact:true}).click();
  await expect(page).toHaveURL(/#p-message-m29$/);
  await expect(page.locator("#p-message-m29")).toBeFocused();
  expect(requests).toHaveLength(1);
  await expect(page.locator(".footnotes")).toContainText("Source vérifiée");
  await expect(page.locator(".p-chart canvas")).toHaveCount(1);
  await page.getByRole("button",{name:"View as table",exact:true}).click();
  await expect(page.locator(".p-chart table")).toContainText("40");
  const download = page.waitForEvent("download");
  await page.locator(".p-table-actions").getByRole("button",{name:"Download Excel",exact:true}).click();
  expect((await download).suggestedFilename()).toBe("pengolo-table.xlsx");
  await page.getByRole("checkbox",{name:"Task 1",exact:true}).check();
  await expect.poll(() => checked).toBe(true);
  await context.grantPermissions(["clipboard-read","clipboard-write"]);
  await page.getByRole("article").last().getByRole("button",{name:"Copy response",exact:true}).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain("- [x] Vérifier la source");
  expect(remoteImages).toEqual([]);
  await page.locator(".p-conversation").evaluate(element => { element.scrollTop = 0; });
  await expect(page.getByRole("article")).toHaveCount(20);
  expect(requests[1]?.variables.before).toBe("m20");
  expect(requests[1]?.query).not.toContain("ChatHeader");
  await page.getByText("Document · eeeeeeee",{exact:true}).scrollIntoViewIfNeeded();
  await expect(page.getByText("Document · eeeeeeee",{exact:true})).toBeVisible();
  await page.reload();
  await expect(page.getByRole("article")).toHaveCount(10);
  await expect(page.getByRole("article").last()).toBeInViewport();
  await expect(page.getByRole("checkbox",{name:"Task 1",exact:true})).toBeChecked();
  const chartCanvas = page.locator(".p-chart canvas");
  await expect(chartCanvas).toHaveCount(1);
  await expect.poll(async () => (await chartCanvas.boundingBox())?.height ?? 0).toBeGreaterThan(250);
  await chartCanvas.scrollIntoViewIfNeeded();
  await page.screenshot({path:"/tmp/results/pengolo-chart.png",fullPage:true});
  await page.getByRole("heading",{name:"Résumé",exact:true}).scrollIntoViewIfNeeded();
  await expect(page.locator(".p-chart canvas")).toHaveCount(1);
  await page.screenshot({path:"/tmp/results/pengolo-desktop.png",fullPage:true});
  await page.setViewportSize({width:390,height:740});
  await expect(page.getByRole("button",{name:"Open Pengolo sidebar",exact:true})).toBeVisible();
  await expect.poll(() => page.locator(".p-sidebar").evaluate(element => element.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  await page.getByRole("heading",{name:"Résumé",exact:true}).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({path:"/tmp/results/pengolo-mobile.png",fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole("button",{name:"Temporary chat",exact:true}).click();
  await expect(page).toHaveURL(/\/pengolo$/);
  await page.locator('[data-tour="search"]').click();
  const storedChatSearch = page.getByRole("dialog",{name:"Search Galadril"});
  await storedChatSearch.getByRole("textbox").fill("History");
  await storedChatSearch.getByText("History",{exact:true}).click();
  await expect(page.getByRole("article")).toHaveCount(10);
  expect(storedChatScopes).toEqual([false]);
  await expect(page.getByRole("button",{name:"Temporary chat",exact:true})).toHaveAttribute("aria-pressed","false");
  authorized = false;
  const reloadedWorkspace = page.waitForResponse(response => response.request().method() === "POST" && response.request().postData()?.includes("query Workspace") === true);
  await page.evaluate(() => window.dispatchEvent(new Event("pengolo-access-invalidated")));
  await reloadedWorkspace;
  await expect(page.getByRole("article")).toHaveCount(0);
  await page.locator('[data-tour="search"]').click();
  await page.getByRole("dialog",{name:"Search Galadril"}).getByRole("textbox").fill("vérifer");
  await expect(page.getByRole("dialog",{name:"Search Galadril"}).getByRole("status",{name:"Searching Galadril"})).toHaveCount(0);
  await expect(page.getByRole("dialog",{name:"Search Galadril"}).getByText("History",{exact:true})).toHaveCount(0);
});
