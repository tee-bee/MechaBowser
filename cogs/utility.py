import asyncio
import logging
import re
import typing
import datetime
import time
import aiohttp

import pymongo
import discord
from discord import Webhook, AsyncWebhookAdapter
from discord.ext import commands, tasks
from fuzzywuzzy import fuzz, process

import config
import utils

mclient = pymongo.MongoClient(
	config.mongoHost,
	username=config.mongoUser,
	password=config.mongoPass
)

serverLogs = None
modLogs = None

class NintenDeals(commands.Cog):
    def __init__(self, bot):
        self.dealsMongo = pymongo.MongoClient(
            config.mongoDealsHost,
            port=config.mongoDealsPort,
            username=config.mongoDealsUser,
            password=config.mongoDealsPass,
            authSource=config.mongoDealsAuth
        )
        self.bot = bot
        self.games = {}
        self.dealMessages = []
        self.gamesReady = False
        self.saleData = None
        self.dealChannel = self.bot.get_channel(613785143059939338)
        self.releaseChannel = self.bot.get_channel(613785177830981635)
        self.session = aiohttp.ClientSession()
        self.creds = {'api_key': config.dealsAPIKey}
        self.codepoints = {
            'CA': '\U0001f1e8\U0001f1e6',
            'MX': '\U0001f1f2\U0001f1fd',
            'US': '\U0001f1fa\U0001f1f8',
            'CZ': '\U0001f1e8\U0001f1ff',
            'DK': '\U0001f1e9\U0001f1f0',
            'EU': '\U0001f1ea\U0001f1fa',
            'GB': '\U0001f1ec\U0001f1e7',
            'NO': '\U0001f1f3\U0001f1f4',
            'PL': '\U0001f1f5\U0001f1f1',
            'RU': '\U0001f1f7\U0001f1fa',
            'ZA': '\U0001f1ff\U0001f1e6',
            'SE': '\U0001f1f8\U0001f1ea',
            'CH': '\U0001f1e8\U0001f1ed',
            'AU': '\U0001f1e6\U0001f1fa',
            'NZ': '\U0001f1f3\U0001f1ff',
            'JP': '\U0001f1ef\U0001f1f5'
        }
        self.query_deals.start() #pylint: disable=no-member
        self.update_game_info.start() #pylint: disable=no-member
        #self.new_release_posting.start() #pylint: disable=no-member
        logging.info('[Deals] NintenDeals task cogs loaded')

    def cog_unload(self):
        logging.info('[Deals] Attempting to cancel tasks...')
        self.query_deals.cancel() #pylint: disable=no-member
        self.update_game_info.cancel() #pylint: disable=no-member
        #self.new_release_posting.cancel() #pylint: disable=no-member
        logging.info('[Deals] Tasks exited')
        #asyncio.get_event_loop().run_until_complete(self.session.close())
        #self.session.close()
        logging.info('[Deals] NintenDeals task cogs unloaded')

    @tasks.loop(seconds=43200)
    async def update_game_info(self):
        logging.debug('[Deals] Starting game fetch')
        gameDB = mclient.bowser.games
        ndealsDB = self.dealsMongo.nintendeals.games

        games = ndealsDB.find({'system': 'Switch'})
        for game in games:
            await asyncio.sleep(0.01)
            scores = {'metascore': game['scores']['metascore'], 'userscore': game['scores']['userscore']}
            gameEntry = {
                    '_id': game['_id'],
                    'nsuids': game['nsuids'],
                    'titles': game['titles'],
                    'release_dates': game['release_dates'],
                    'categories': game['categories'],
                    'websites': game['websites'],
                    'scores': scores,
                    'free_to_play': game['free_to_play']
                }
            ourGame = gameDB.find_one({'_id': game['_id']})
            self.games[game['_id']] = gameEntry

            if not ourGame:
                gameEntry['released'] = False # New game. Force new_release_posting to check if it's released, and if so post it
                gameDB.insert_one(gameEntry)

            else:
                comparison = {
                    '_id': ourGame['_id'],
                    'nsuids': ourGame['nsuids'],
                    'titles': ourGame['titles'],
                    'release_dates': ourGame['release_dates'],
                    'categories': ourGame['categories'],
                    'websites': ourGame['websites'],
                    'scores': ourGame['scores'],
                    'free_to_play': ourGame['free_to_play']
                }
                if comparison != gameEntry:
                    logging.debug(f'Updating out of date game entry {ourGame["_id"]}')
                    gameDB.update_one({'_id': ourGame['_id']}, {'$set': comparison})

        self.gamesReady = True

    @tasks.loop(seconds=60)
    async def new_release_posting(self):
        logging.info('[Deals] Starting new releases check')
        db = mclient.bowser.games
        if not self.gamesReady: return # Wait until next pass so game list can update

        for game in db.find({'released': False}):
            nowReleased = False
            regionalDates = {}
            for key, value in game['release_dates'].items():
                if value == None:
                    regionalDates[key] = '*Not currently set to release in this region*'
                    continue

                if value < datetime.datetime.utcnow(): # Release date has now passed
                    nowReleased = True

                regionalDates[key] = value.strftime("%B %d, %Y at %H:%M UTC")

            if not nowReleased: # Game has not yet released yet
                continue

            if game['titles']['NA'] != None:
                name = game['titles']['NA']

            elif game['titles']['EU'] != None:
                name = game['titles']['EU']

            else:
                name = game['titles']['JP']

            # Really annoying way to have priority over what site is used
            websites = game['websites']
            siteUrl = None
            if websites['US']: siteUrl = websites['US']
            elif websites['CA']: siteUrl = websites['CA']
            elif websites['EU']: siteUrl = websites['EU']
            elif websites['GB']: siteUrl = websites['GB']
            elif websites['AU']: siteUrl = websites['AU']
            elif websites['NZ']: siteUrl = websites['NZ']
            elif websites['JP']: siteUrl = websites['JP']
            elif websites['CH']: siteUrl = websites['CH']
            elif websites['RU']: siteUrl = websites['RU']
            elif websites['ZA']: siteUrl = websites['ZA']

            gameDetails = await utils.scrape_nintendo(siteUrl)
            
            if len(gameDetails['description']) > 2048: gameDetails['description'] = f'{gameDetails["description"][:2045]}...'
            #strDetails = ':book: **Genre:** {}\n'.format(gameDetails['category'])
            strDetails = ':thought_balloon: **Developer:** {}\n'.format(gameDetails['manufacturer'])
            strDetails += ':postal_horn: **Publisher:** {}'.format(gameDetails['brand'])

            if gameDetails['romSize']:
                strDetails += '\n:page_facing_up: **File Size:** {}'.format(gameDetails['romSize'])

            embed = discord.Embed(title=name, description=gameDetails['description'], color=0x7ED321)
            
            embed.set_thumbnail(url=gameDetails['image'])
            embed.add_field(name='Game Details', value=strDetails)
            await self.releaseChannel.send(embed=embed)

    @tasks.loop(seconds=14400)
    async def query_deals(self):
        logging.debug('[Deals] Starting deals check')
        for x in self.dealMessages:
            await x.delete()

        self.dealMessages = []

        async with self.session.get(config.dealsAPI, params=self.creds) as r:
            if r.status != 200:
                logging.error(f'[Deals] NintenDeals API returned non-OK code {r.status}')
                return

            try:
                resp = await r.json()
                self.saleData = resp

            except Exception as e:
                logging.error(f'[Deals] Error while retrieving deals json: {e}')

            message = f'**Nintendo Switch Game Deals**\nLast updated {datetime.datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")}\n\n' \
                'This deals list is updated 4 times each day to contain the 20 top metascore rated games currently on sale. \n' \
                '> Note: This list only includes games which have prices in USD\n' \
                '> Game sale date provided gratefully by <http://www.nintendeals.xyz/>' \
                '\n{}'.format('-' * 30)
                # Remove newlines above
                #'You can search any game on Nintendo Switch, even it it is not currently on sale, with the `!games search Name` command ' \
                #'replacing "Name" with the name of the game\n' \
            games = []

            maxAmt = 20
            gameInfo = {}
            gameScore = {}

            for x in resp['games_on_sale']:
                await asyncio.sleep(0.01)
                title = None
                if 'NA' in x['titles'].keys():
                    title = x['titles']['NA'] if x['titles']['NA'] else None

                if not title and 'EU' in x['titles'].keys():
                    title = x['titles']['EU'] if x['titles']['EU'] else None

                if not title:
                    continue # Ignore if no english title was set by this point

                if not 'US' in x['price'].keys():
                    continue # Ignore if there is no USD price

                gameInfo[title] = x
                gameScore[title] = 0 if x['scores']['metascore'] == '-' else x['scores']['metascore']

            sortedScores = sorted(gameScore.items(), key=lambda kv: kv[1], reverse=True)
            for y in sortedScores:
                if maxAmt <= 0: break
                x = gameInfo[y[0]]

                MS = 'N/a' if x['scores']['metascore'] == '-' else x['scores']['metascore']
                US = 'N/a' if x['scores']['userscore'] == '-' else x['scores']['userscore']
                gameText = ''

                if 'NA' in x['titles'].keys():
                    title = x['titles']['NA'] if x['titles']['NA'] else None

                if not title and 'EU' in x['titles'].keys():
                    title = x['titles']['EU'] if x['titles']['EU'] else None

                gameText += f'**{title}**\n<:barchart:612724385505083392> ___Metascore:___ *{MS}* ___Userscore:___ *{US}*\n'

                entry = 0
                for key, value in x['price'].items():
                    if key not in ['US', 'EU', 'GB', 'AU']: continue
                    if 'discount' not in value.keys(): continue # Game not on sale in that region

                    entry += 1
                    if entry == 3:
                        # Make second row
                        gameText += '\n'
                    #print(value)
                    gameText += f'{self.codepoints[key]} {resp["countries"][key]["currency"]}{value["sale_price"]} (-{value["discount"]}%) '

                if entry == 0:
                    continue # There no prices in the regions we want

                maxAmt -= 1
                games.append('​\n\n' + gameText) # Add zero-length and newline

            chunk = message
            num = 0
            for x in games:
                if len(chunk) + len(x) <= 1990:
                    chunk += x
                    continue

                if num >= 1:
                    chunk = '​' + chunk[2:] # Remove extra new lines at beginning of new message

                self.dealMessages.append(await self.dealChannel.send(chunk))
                chunk = x
                num += 1

            if num >= 1:
                chunk = '​' + chunk[2:] # Remove extra new lines at beginning of new message

            self.dealMessages.append(await self.dealChannel.send(chunk))

    @commands.has_any_role(config.moderator, config.eh)
    @commands.group(name='games')
    async def _games(self, ctx):
        return

    @commands.has_any_role(config.moderator, config.eh)
    @_games.command(name='search')
    async def _games_search(self, ctx, *, game):
        db = mclient.bowser.games
        dealprices = self.dealsMongo.nintendeals.prices
        gameID = ''
        gameName = ''
        fuzzyList = []
        titleList = []

        for x in self.games.values():
            titles = []

            for y in x['titles'].values():
                if y == None: continue
                if y in fuzzyList: continue

                fuzzyList.append(y)
                titles.append(y)

            titleList.append({x['_id']: titles})

        if game.upper() in (x.upper() for x in fuzzyList):
            done = False
            for n in titleList:
                for key, value in n.items():
                    for y in value:
                        if game.upper() == y.upper():
                            gameID = key
                            gameName = y
                            done = True
                            break

                if done: break

        else:
            results = process.extract(game, fuzzyList, limit=3, scorer=fuzz.partial_ratio)

            if results[0][1] <= 85:
                embed = discord.Embed(title='No game found', description=f'Unable to find a game with the title of **{game}**. Did you mean...\n\n' \
                f'*{results[0][0]}\n{results[1][0]}\n{results[2][0]}*', color=0xCF675A, timestamp=datetime.datetime.utcnow())

                return await ctx.send(ctx.author.mention, embed=embed)

            else:
                for n in titles:
                    print(n)
                    for key, value in n:
                        if results[0][0].upper() == value.upper():
                            gameName = value
                            gameID = key
                            done = True
                            break

                    if done: break

        if not gameID or not gameName:
            # Not sure why/if ever this should call, but safety is key
            logging.error('[Deals] No gameid or name!')
            return await ctx.send(f'{config.redTick} An error occured while searching for that game. If this keeps happening let a staff member know')

        doc = db.find_one({'_id': gameID})
        prices = dealprices.find({'game_id': gameID})

        if not prices.count():
            # Such as unreleased games
            desc = '*This is no available price data for this game*'

        else:
            desc = 'Price data:\n\n'
            gamePrices = {}
            for x in self.saleData['games_on_sale']:
                if x['titles'] == doc['titles']:
                    for key, value in x['price'].items():
                        gamePrices[key] = {
                            'discount': value['discount'],
                            'sale_price': value['sale_price'],
                            'price': value['full_price']
                        }

                    break

            for country in prices:
                for key, value in country['prices'].items():
                    if key in gamePrices.keys(): continue
                    gamePrices[key] = {
                        'discount': None,
                        'sale_price': None,
                        'price': value['full_price']
                    }

            entry = 0
            for key, value in gamePrices.items():
                currency = self.saleData["countries"][key]["currency"]
                entry += 1
                if entry == 3:
                    desc += '\n'
                    entry = 1

                if value['discount']:
                    desc += f'[{self.codepoints[key]} ~~{currency}{value["price"]}~~ {currency}{value["sale_price"]} (-{value["discount"]}%)]({prices[key]}) '

                else:
                    desc += f'{self.codepoints[key]} {currency}{value["price"]} '

        embed = discord.Embed(title=gameName, color=0x50E3C2, description=desc)
        await ctx.send(embed=embed)


class ChatControl(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.modLogs = self.bot.get_channel(config.modChannel)
        self.linkRe = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        self.SMM2LevelID = re.compile(r'([0-9a-z]{3}-[0-9a-z]{3}-[0-9a-z]{3})', re.I | re.M)
        self.SMM2LevelPost = re.compile(r'Name: ?(\S.*)\n\n?(?:Level )?ID:\s*((?:[0-9a-z]{3}-){2}[0-9a-z]{3})(?:\s+)?\n\n?Style: ?(\S.*)\n\n?(?:Theme: ?(\S.*)\n\n?)?(?:Tags: ?(\S.*)\n\n?)?Difficulty: ?(\S.*)\n\n?Description: ?(\S.*)', re.I)
        self.affiliateLinks = re.compile(r'(https?:\/\/(?:.*\.)?(?:(?:amazon)|(?:bhphotovideo)|(?:bestbuy)|(?:ebay)|(?:gamestop)|(?:groupon)|(?:newegg(?:business)?)|(?:stacksocial)|(?:target)|(?:tigerdirect)|(?:walmart))\.[a-z\.]{2,7}\/.*)(?:\?.+)', re.I)
        self.thirtykEvent = {}
        self.thirtykEventRoles = [
            616298509460701186,
            616298665421701128,
            616298689044152335,
            616298709860220928,
            616298733642186769,
            616298767108407335,
            616298787761291267,
            616298809454100480,
            616298829830160430,
            616298851900456991
            ]

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.type != discord.MessageType.default:
            return

        #Filter test for afiliate links
        if re.search(self.affiliateLinks, message.content):
            hooks = await message.channel.webhooks()
            useHook = await message.channel.create_webhook(name=f'mab_{message.channel.id}', reason='No webhooks existed; 1<= required for chat filtering') if not hooks else hooks[0]

            await message.delete()
            async with aiohttp.ClientSession() as session:
                name = message.author.name if not message.author.nick else message.author.nick
                webhook = Webhook.from_url(useHook.url, adapter=AsyncWebhookAdapter(session))
                await webhook.send(content=re.sub(self.affiliateLinks, r'\1', message.content), username=name, avatar_url=message.author.avatar_url)

        #Filter for #mario
        if message.channel.id == 325430144993067049: # #mario
            if re.search(self.SMM2LevelID, message.content):
                if re.search(self.linkRe, message.content):
                    return # TODO: Check if SMM2LevelID found in linkRe to correct edge case

                await message.delete()
                response = await message.channel.send(f'<:redTick:402505117733224448> <@{message.author.id}> Please do not post Super Mario Maker 2 level codes '\
                    'here. Post in <#595203237108252672> with the pinned template instead.')

                await response.delete(delay=20)
            return

        #Filter for #smm2-levels
        if message.channel.id == 595203237108252672:
            if not re.search(self.SMM2LevelID, message.content):
                # We only want to filter posts with a level id
                return

            block = re.search(self.SMM2LevelPost, message.content)
            if not block:
                # No match for a properly formatted level post
                response = await message.channel.send(f'<:redTick:402505117733224448> <@{message.author.id}> Your level is formatted incorrectly, please see the pinned messages for the format. A copy '\
                    f'of your message is included and will be deleted shortly. You can resubmit your level at any time.\n\n```{message.content}```')
                await message.delete()
                return await response.delete(delay=25)

            # Lets make this readable
            levelName = block.group(1)
            levelID = block.group(2)
            levelStyle = block.group(3)
            levelTheme = block.group(4)
            levelTags = block.group(5)
            levelDifficulty = block.group(6)
            levelDescription = block.group(7)

            embed = discord.Embed(color=discord.Color(0x6600FF))
            embed.set_author(name=str(message.author), icon_url=message.author.avatar_url)
            embed.add_field(name='Name', value=levelName, inline=True)
            embed.add_field(name='Level ID', value=levelID, inline=True)
            embed.add_field(name='Description', value=levelDescription, inline=False)
            embed.add_field(name='Style', value=levelStyle, inline=True)
            embed.add_field(name='Difficulty', value=levelDifficulty, inline=True)
            if levelTheme:
                embed.add_field(name='Theme', value=levelTheme, inline=False)
            if levelTags:
                embed.add_field(name='Tags', value=levelTags, inline=False)

            try:
                await message.channel.send(embed=embed)
                await message.delete()

            except discord.errors.Forbidden:
                # Fall back to leaving user text
                logging.error(f'[Filter] Unable to send embed to {message.channel.id}')
            return

#        # 30k members celebration - ended 9/6/19
#        if message.author.id not in self.thirtykEvent.keys() or (self.thirtykEvent[message.author.id] + 120) <= time.time():
#            import random
#            self.thirtykEvent[message.author.id] = time.time()
#            newRole = random.choice(self.thirtykEventRoles)
#            for role in message.author.roles:
#                if role.id in self.thirtykEventRoles:
#                    await message.author.remove_roles(role)
#
#            await message.author.add_roles(message.guild.get_role(newRole))

#        # Splatoon splatfest event - ended 7/21/19
#        if message.channel.id == 278557283019915274:
#            pearl = re.compile(r'(<:pearl:332557519958310912>)+', re.I)
#            marina = re.compile(r'(<:marina:332557579815485451>)+', re.I)
#            orderRole = message.guild.get_role(601458524723216385)
#            chaosRole = message.guild.get_role(601458570197860449)
#            if re.search(pearl, message.content) and re.search(marina, message.content):
#                return
#
#            try:    
#                if re.search(pearl, message.content):
#                    if orderRole in message.author.roles:
#                        await message.author.remove_roles(orderRole)
#
#                    if chaosRole not in message.author.roles:
#                        msg = await message.channel.send(f'<@{message.author.id}> You are now registered as a member of Team Chaos')
#                        await msg.delete(delay=5.0)
#                        await message.author.add_roles(chaosRole)
#
#                elif re.search(marina, message.content):
#                    if chaosRole in message.author.roles:
#                        await message.author.remove_roles(chaosRole)
#
#                    if orderRole not in message.author.roles:
#                        msg = await message.channel.send(f'<@{message.author.id}> You are now registered as a member of Team Order')
#                        await msg.delete(delay=5.0)
#                        await message.author.add_roles(orderRole)
#
#            except (discord.Forbidden, discord.HTTPException):
#                pass

    @commands.command(name='ping')
    async def _ping(self, ctx):
        initiated = ctx.message.created_at
        msg = await ctx.send('Evaluating...')
        return await msg.edit(content=f'Pong! Roundtrip latency {(msg.created_at - initiated).total_seconds()} seconds')

    @commands.command(name='clean')
    @commands.has_any_role(config.moderator, config.eh)
    async def _clean(self, ctx, messages: int, members: commands.Greedy[discord.Member]):
        if messages >= 100:
            def confirm_check(reaction, member):
                return member == ctx.author and str(reaction.emoji) in [config.redTick, config.greenTick]

            confirmMsg = await ctx.send(f'This action will delete up to {messages}, are you sure you want to proceed?')
            await confirmMsg.add_reaction(config.greenTick)
            await confirmMsg.add_reaction(config.redTick)
            try:
                reaction = await self.bot.wait_for('reaction_add', timeout=15, check=confirm_check)
                if str(reaction[0]) != config.greenTick:
                    await confirmMsg.edit(content='Clean action canceled.')
                    return await confirmMsg.clear_reactions()

            except asyncio.TimeoutError:
                await confirmMsg.edit(content='Confirmation timed out, clean action canceled.')
                return await confirmMsg.clear_reactions()

            else:
                await confirmMsg.delete()
            
        memberList = None if not members else [x.id for x in members]

        def message_filter(message):
            return True if not memberList or message.author.id in memberList else False

        await ctx.message.delete()
        deleted = await ctx.channel.purge(limit=messages, check=message_filter, bulk=True)
    
        m = await ctx.send(f'{config.greenTick} Clean action complete')
        archiveID = await utils.message_archive(list(reversed(deleted)))

        embed = discord.Embed(description=f'Archive URL: {config.baseUrl}/archive/{archiveID}', color=0xF5A623, timestamp=datetime.datetime.utcnow())
        await self.bot.get_channel(config.logChannel).send(f':printer: New message archive generated for {ctx.channel.mention}', embed=embed)

        return await m.delete(delay=5)

    @commands.command(name='info')
    @commands.has_any_role(config.moderator, config.eh)
    async def _info(self, ctx, user: typing.Union[discord.Member, int]):
        inServer = True
        if type(user) == int:
            # User doesn't share the ctx server, fetch it instead
            dbUser = mclient.bowser.users.find_one({'_id': user})
            inServer = False
            try:
                user = await self.bot.fetch_user(user)

            except discord.NotFound:
                return await ctx.send(f'{config.redTick} User does not exist')

            if not dbUser:
                embed = discord.Embed(color=discord.Color(0x18EE1C), description=f'Fetched information about {user.mention} from the API because they are not in this server. There is little information to display as such')
                embed.set_author(name=f'{str(user)} | {user.id}', icon_url=user.avatar_url)
                embed.set_thumbnail(url=user.avatar_url)
                embed.add_field(name='Created', value=user.created_at.strftime('%B %d, %Y %H:%M:%S UTC'))
                return await ctx.send(embed=embed) # TODO: Return DB info if it exists as well

        else:
            dbUser = mclient.bowser.users.find_one({'_id': user.id})

        # Member object, loads of info to work with
        messages = mclient.bowser.messages.find({'author': user.id})
        msgCount = 0 if not messages else messages.count()

        desc = f'Fetched user {user.mention}' if inServer else f'Fetched information about previous member {user.mention} ' \
            'from the API because they are not in this server. ' \
            'Showing last know data from before they left.'
        embed = discord.Embed(color=discord.Color(0x18EE1C), description=desc)
        embed.set_author(name=f'{str(user)} | {user.id}', icon_url=user.avatar_url)
        embed.set_thumbnail(url=user.avatar_url)
        embed.add_field(name='Messages', value=str(msgCount), inline=True)
        if inServer:
            embed.add_field(name='Join date', value=user.joined_at.strftime('%B %d, %Y %H:%M:%S UTC'), inline=True)
        roleList = []
        if inServer:
            for role in reversed(user.roles):
                if role.id == user.guild.id:
                    continue

                roleList.append(role.name)

        else:
            roleList = dbUser['roles']
            
        if not roleList:
            # Empty; no roles
            roles = '*User has no roles*'

        else:
            if not inServer:
                tempList = []
                for x in reversed(roleList):
                    y = ctx.guild.get_role(x)
                    name = '*deleted role*' if not y else y.name
                    tempList.append(name)

                roleList = tempList

            roles = ', '.join(roleList)

        embed.add_field(name='Roles', value=roles, inline=False)

        lastMsg = 'N/a' if msgCount == 0 else datetime.datetime.utcfromtimestamp(messages.sort('timestamp',pymongo.DESCENDING)[0]['timestamp']).strftime('%B %d, %Y %H:%M:%S UTC')
        embed.add_field(name='Last message', value=lastMsg, inline=True)
        embed.add_field(name='Created', value=user.created_at.strftime('%B %d, %Y %H:%M:%S UTC'), inline=True)
        punishments = ''
        punsCol = mclient.bowser.puns.find({'user': user.id})
        if not punsCol.count():
            punishments = '__*No punishments on record*__'

        else:
            puns = 0
            for pun in punsCol.sort('timestamp', pymongo.DESCENDING):
                if puns >= 5:
                    break

                puns += 1
                stamp = datetime.datetime.utcfromtimestamp(pun['timestamp']).strftime('%m/%d/%y %H:%M:%S UTC')
                punType = config.punStrs[pun['type']]
                if pun['type'] in ['clear', 'unmute', 'unban', 'unblacklist']:
                    punishments += f'- [{stamp}] {punType}\n'

                else:
                    punishments += f'+ [{stamp}] {punType}\n'

            punishments = f'Showing {puns}/{punsCol.count()} punishment entries. ' \
                f'For a full history including responsible moderator, active status, and more use `{ctx.prefix}history @{str(user)}` or `{ctx.prefix}history {user.id}`' \
                f'\n```diff\n{punishments}```'
        embed.add_field(name='Punishments', value=punishments, inline=False)
        return await ctx.send(embed=embed)

    @commands.command(name='history')
    @commands.has_any_role(config.moderator, config.eh)
    async def _history(self, ctx, user: typing.Union[discord.User, int]):
        if type(user) == int:
            # User doesn't share the ctx server, fetch it instead
            try:
                user = await self.bot.fetch_user(user)

            except discord.NotFound:
                return await ctx.send(f'{config.redTick} User does not exist')

        db = mclient.bowser.puns
        puns = db.find({'user': user.id})
        if not puns.count():
            return await ctx.send(f'{config.redTick} User has no punishments on record')

        punNames = {
            'tier1': 'T1 Warn',
            'tier2': 'T2 Warn',
            'tier3': 'T3 Warn',
            'clear': 'Warn Clear',
            'mute': 'Mute',
            'unmute': 'Unmute',
            'kick': 'Kick',
            'ban': 'Ban',
            'unban': 'Unban',
            'blacklist': 'Blacklist ({})',
            'unblacklist': 'Unblacklist ({})'
        }

        if puns.count() == 1:
            desc = f'There is __1__ infraction record for this user:'

        else:
            desc = f'There are __{puns.count()}__ infraction records for this user:'

        embed = discord.Embed(title='Infraction History', description=desc, color=0x18EE1C)
        embed.set_author(name=f'{user} | {user.id}', icon_url=user.avatar_url)

        for pun in puns.sort('timestamp', pymongo.DESCENDING):
            datestamp = datetime.datetime.utcfromtimestamp(pun['timestamp']).strftime('%b %d, %y %H:%M UTC')
            moderator = ctx.guild.get_member(pun['moderator'])
            if not moderator:
                moderator = await self.bot.fetch_user(pun['moderator'])

            if pun['type'] in ['blacklist', 'unblacklist']:
                inf = punNames[pun['type']].format(pun['context'])

            else:
                inf = punNames[pun['type']]

            embed.add_field(name=datestamp, value=f'**Moderator:** {moderator}\n**Details:** [{inf}] {pun["reason"]}')

        return await ctx.send(embed=embed)
            

    @commands.command(name='roles')
    @commands.has_any_role(config.moderator, config.eh)
    async def _roles(self, ctx):
        roleList = 'List of roles in guild:\n```\n'
        for role in reversed(ctx.guild.roles):
            roleList += f'{role.name} ({role.id})\n'

        await ctx.send(f'{roleList}```')

    @commands.command(name='blacklist')
    @commands.has_any_role(config.moderator, config.eh)
    async def _roles_set(self, ctx, member: discord.Member, channel: discord.TextChannel, *, reason='-No reason specified-'):
        statusText = ''
        if channel.id == config.suggestions:
            suggestionsRole = ctx.guild.get_role(config.noSuggestions)
            if suggestionsRole in member.roles: # Toggle role off
                await member.remove_roles(suggestionsRole)
                statusText = 'Unblacklisted'

            else: # Toggle role on
                await member.add_roles(suggestionsRole)
                statusText = 'Blacklisted'

        elif channel.id == config.spoilers:
            spoilersRole = ctx.guild.get_role(config.noSpoilers)
            if spoilersRole in member.roles: # Toggle role off
                await member.remove_roles(spoilersRole)
                statusText = 'Unblacklisted'

            else: # Toggle role on
                await member.add_roles(spoilersRole)
                statusText = 'Blacklisted'         

        else:
            return await ctx.send(f'{config.redTick} You cannot blacklist a user from that channel')

        db = mclient.bowser.puns
        if statusText.lower() == 'blacklisted':
            await utils.issue_pun(member.id, ctx.author.id, 'blacklist', reason, context=channel.name)

        else:
            db.find_one_and_update({'user': member.id, 'type': 'blacklist', 'active': True, 'context': channel.name}, {'$set':{
            'active': False
            }})
            await utils.issue_pun(member.id, ctx.author.id, 'unblacklist', reason, active=False, context=channel.name)

        embed = discord.Embed(color=discord.Color(0xF5A623), timestamp=datetime.datetime.utcnow())
        embed.set_author(name=f'{statusText} | {str(member)}')
        embed.add_field(name='User', value=f'<@{member.id}>', inline=True)
        embed.add_field(name='Moderator', value=f'<@{ctx.author.id}>', inline=True)
        embed.add_field(name='Channel', value=channel.mention)
        embed.add_field(name='Reason', value=reason)

        await self.modLogs.send(embed=embed)

        try:
            statusText = 'blacklist' if statusText == 'Blacklisted' else 'unblacklist'
            await member.send(utils.format_pundm(statusText, reason, ctx.author, channel.mention))
        except (discord.Forbidden, AttributeError): # User has DMs off, or cannot send to Obj
            pass

        if await utils.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

        await ctx.send(f'{config.greenTick} {member} has been {statusText.lower()}ed from {channel.mention}')

def setup(bot):
    global serverLogs
    global modLogs

    serverLogs = bot.get_channel(config.logChannel)
    modLogs = bot.get_channel(config.modChannel)

    bot.add_cog(ChatControl(bot))
    bot.add_cog(NintenDeals(bot))
    logging.info('[Extension] Utility module loaded')

def teardown(bot):
    bot.remove_cog('ChatControl')
    bot.remove_cog('NintenDeals')
    logging.info('[Extension] Utility module unloaded')