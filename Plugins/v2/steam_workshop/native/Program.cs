using System.Text.Json;
using System.IO.Compression;
using ProtoBuf;
using SteamKit2;
using SteamKit2.Internal;

var client = new SteamClient(SteamConfiguration.Create(b => b.WithProtocolTypes(ProtocolTypes.WebSocket)));
var callbacks = new CallbackManager(client);
var user = client.GetHandler<SteamUser>()!;
var service = client.GetHandler<SteamUnifiedMessages>()!.CreateService<PublishedFile>();
var login = new TaskCompletionSource<EResult>(TaskCreationOptions.RunContinuationsAsynchronously);
using var stop = new CancellationTokenSource();
string? token = null;
var authenticated = args.Length == 3 && args[0] == "--account";
callbacks.Subscribe<SteamClient.ConnectedCallback>(_ => {
    if (authenticated) user.LogOn(new SteamUser.LogOnDetails {
        Username = args[2], AccessToken = token, ShouldRememberPassword = true,
        LoginID = (uint)Environment.ProcessId,
    });
    else user.LogOnAnonymous();
});
callbacks.Subscribe<SteamUser.LoggedOnCallback>(c => login.TrySetResult(c.Result));
callbacks.Subscribe<SteamClient.DisconnectedCallback>(_ => {
    login.TrySetException(new IOException("Steam disconnected. Please retry."));
    stop.Cancel();
});
var pump = Task.Run(() => {
    while (!stop.IsCancellationRequested) callbacks.RunWaitCallbacks(TimeSpan.FromMilliseconds(100));
});
try
{
    if (authenticated)
    {
        using var file = File.OpenRead(args[1]);
        using var compressed = new DeflateStream(file, CompressionMode.Decompress);
        var account = Serializer.Deserialize<SavedAccount>(compressed);
        token = account.LoginTokens.FirstOrDefault(x =>
            string.Equals(x.Key, args[2], StringComparison.OrdinalIgnoreCase)).Value;
        if (string.IsNullOrEmpty(token)) throw new IOException("Sign in to Steam again before subscribing.");
    }
    client.Connect();
    var loggedOn = await login.Task.WaitAsync(TimeSpan.FromSeconds(40));
    if (authenticated && loggedOn != EResult.OK)
        throw new IOException($"Steam sign-in returned {loggedOn}. Sign in to Steam again before subscribing.");
    Require(loggedOn);
    while (!stop.IsCancellationRequested && await Console.In.ReadLineAsync(stop.Token) is { } line)
    {
        try
        {
            using var document = JsonDocument.Parse(line);
            var request = document.RootElement;
            var appId = request.GetProperty("appid").GetUInt32();
            if (appId == 0) throw new ArgumentException("Invalid Steam App ID.");
            object response;
            switch (request.GetProperty("op").GetString())
            {
                case "subscriptions":
                    if (!authenticated) throw new IOException("Sign in to Steam before checking subscriptions.");
                    var membership = new CPublishedFile_AreFilesInSubscriptionList_Request { appid = appId, listtype = 1 };
                    foreach (var id in request.GetProperty("ids").EnumerateArray())
                        membership.publishedfileids.Add(ulong.Parse(id.GetString()!));
                    if (membership.publishedfileids.Count is < 1 or > 100)
                        throw new ArgumentException("Expected 1 to 100 Workshop IDs.");
                    var memberships = await service.AreFilesInSubscriptionList(membership).ToTask().WaitAsync(TimeSpan.FromSeconds(25), stop.Token);
                    Require(memberships.Result);
                    response = new { items = memberships.Body.files.Select(x => new {
                        id = x.publishedfileid.ToString(), subscribed = x.inlist,
                    }) };
                    break;
                case "subscribe":
                case "unsubscribe":
                    if (!authenticated) throw new IOException("Sign in to Steam before subscribing.");
                    var itemId = ulong.Parse(request.GetProperty("id").GetString()!);
                    if (itemId == 0) throw new ArgumentException("Invalid Workshop item ID.");
                    var check = new CPublishedFile_GetDetails_Request { appid = appId };
                    check.publishedfileids.Add(itemId);
                    var checkedItem = await service.GetDetails(check).ToTask().WaitAsync(TimeSpan.FromSeconds(25), stop.Token);
                    Require(checkedItem.Result);
                    var item = checkedItem.Body.publishedfiledetails.Single();
                    if (item.result != 1 || item.consumer_appid != appId || item.banned || item.file_type != 0)
                        throw new IOException("This Workshop item is unavailable or belongs to another game.");
                    if (request.GetProperty("op").GetString() == "unsubscribe")
                    {
                        var removal = await service.Unsubscribe(new CPublishedFile_Unsubscribe_Request {
                            publishedfileid = itemId, appid = checked((int)appId), list_type = 1, notify_client = true,
                        }).ToTask().WaitAsync(TimeSpan.FromSeconds(25), stop.Token);
                        Require(removal.Result);
                    }
                    else
                    {
                        var subscription = await service.Subscribe(new CPublishedFile_Subscribe_Request {
                            publishedfileid = itemId, appid = checked((int)appId), list_type = 1,
                            notify_client = true, include_dependencies = false,
                        }).ToTask().WaitAsync(TimeSpan.FromSeconds(25), stop.Token);
                        Require(subscription.Result);
                    }
                    response = new { id = itemId.ToString() };
                    break;
                case "query":
                    var query = new CPublishedFile_QueryFiles_Request {
                        appid = appId, numperpage = Number(request, "page_size", 24),
                        query_type = request.GetProperty("sort").GetUInt32(),
                        search_text = request.GetProperty("search").GetString() ?? "",
                        filetype = 0, return_children = true, return_details = true,
                        return_tags = true, return_previews = true, return_vote_data = true,
                        return_short_description = true,
                    };
                    if (request.TryGetProperty("page", out var page)) query.page = page.GetUInt32();
                    else query.cursor = request.GetProperty("cursor").GetString() ?? "*";
                    if (query.numperpage is < 1 or > 100 || query.page > 1000)
                        throw new ArgumentException("Unsupported Workshop page or page size.");
                    if (query.query_type == 3) query.days = 7;
                    var tag = request.TryGetProperty("tag", out var singleTag) ? singleTag.GetString() : null;
                    if (!string.IsNullOrWhiteSpace(tag)) query.requiredtags.Add(tag);
                    query.requiredtags.AddRange(Strings(request, "tags"));
                    query.excludedtags.AddRange(Strings(request, "excluded_tags"));
                    query.match_all_tags = !request.TryGetProperty("match_all", out var match) || match.GetBoolean();
                    var since = Number(request, "since", 0);
                    if (since > 0) query.date_range_created = new() { timestamp_start = since };
                    var listing = await service.QueryFiles(query).ToTask().WaitAsync(TimeSpan.FromSeconds(25), stop.Token);
                    Require(listing.Result);
                    response = new { total = listing.Body.total, cursor = listing.Body.next_cursor,
                        items = listing.Body.publishedfiledetails.Select(Summary) };
                    break;
                case "author":
                case "subscribed":
                    var subscribedOnly = request.GetProperty("op").GetString() == "subscribed";
                    if (subscribedOnly && !authenticated) throw new IOException("Sign in to Steam to view subscribed mods.");
                    var author = new CPublishedFile_GetUserFiles_Request {
                        appid = appId, steamid = subscribedOnly ? client.SteamID!.ConvertToUInt64() :
                            ulong.Parse(request.GetProperty("author").GetString()!),
                        page = Number(request, "page", 1), numperpage = Number(request, "page_size", 24),
                        type = subscribedOnly ? "mysubscriptions" : "myfiles", sortmethod = "lastupdated", filetype = 0,
                        return_tags = true, return_previews = true,
                        return_children = true, return_vote_data = true,
                    };
                    if (author.page is < 1 or > 1000 || author.numperpage is < 1 or > 100)
                        throw new ArgumentException("Unsupported Workshop page or page size.");
                    author.requiredtags.AddRange(Strings(request, "tags"));
                    author.excludedtags.AddRange(Strings(request, "excluded_tags"));
                    var authorFiles = await service.GetUserFiles(author).ToTask().WaitAsync(TimeSpan.FromSeconds(25), stop.Token);
                    Require(authorFiles.Result);
                    response = new { total = authorFiles.Body.total,
                        items = authorFiles.Body.publishedfiledetails.Select(Summary) };
                    break;
                case "details":
                    var details = new CPublishedFile_GetDetails_Request {
                        appid = appId, includechildren = true, includetags = true,
                        includeadditionalpreviews = true, includevotes = true,
                    };
                    foreach (var id in request.GetProperty("ids").EnumerateArray())
                        details.publishedfileids.Add(ulong.Parse(id.GetString()!));
                    if (details.publishedfileids.Count is < 1 or > 100)
                        throw new ArgumentException("Expected 1 to 100 Workshop IDs.");
                    var result = await service.GetDetails(details).ToTask().WaitAsync(TimeSpan.FromSeconds(25), stop.Token);
                    Require(result.Result);
                    response = new { items = result.Body.publishedfiledetails.Select(Summary) };
                    break;
                default:
                    throw new ArgumentException("Unknown Workshop operation.");
            }
            Emit(new { ok = true, data = response });
        }
        catch (Exception ex)
        {
            Emit(new { ok = false, error = ex.Message });
        }
    }
}
catch (Exception ex)
{
    if (!stop.IsCancellationRequested) Emit(new { ok = false, error = ex.Message });
}
finally
{
    stop.Cancel();
    client.Disconnect();
    await pump;
}

static object Summary(PublishedFileDetails item) => new {
    id = item.publishedfileid.ToString(), appid = item.consumer_appid, item.title,
    item.result, item.file_type, item.file_size, item.time_updated, item.time_created,
    item.preview_url, description = item.short_description.Length > 0 ? item.short_description : item.file_description,
    item.banned, item.subscriptions,
    creator = item.creator.ToString(), item.views, item.favorited,
    item.maybe_inappropriate_sex, item.maybe_inappropriate_violence,
    previews = item.previews.Where(x => x.preview_type == 0).Select(x => x.url),
    votes = item.vote_data?.votes_up ?? 0,
    tags = item.tags.Select(x => x.tag),
    children = item.children.Select(x => x.publishedfileid.ToString()),
};
static uint Number(JsonElement request, string key, uint fallback) =>
    request.TryGetProperty(key, out var value) ? value.GetUInt32() : fallback;
static IEnumerable<string> Strings(JsonElement request, string key) =>
    request.TryGetProperty(key, out var value) ? value.EnumerateArray().Select(x => x.GetString()!).ToArray() : [];
static void Require(EResult result)
{
    if (result != EResult.OK) throw new IOException($"Steam returned {result}. Please retry.");
}
static void Emit(object value)
{
    Console.WriteLine(JsonSerializer.Serialize(value));
    Console.Out.Flush();
}

[ProtoContract]
sealed class SavedAccount
{
    [ProtoMember(4)]
    public Dictionary<string, string> LoginTokens { get; set; } = new();
}
