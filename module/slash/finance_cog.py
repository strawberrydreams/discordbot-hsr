import discord
from discord import app_commands
from discord.ext import commands
import yfinance as yf
from datetime import datetime
import pytz

class FinanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Ticker configuration
        self.tickers = {
            "S&P 500": "^GSPC",
            "나스닥 종합": "^IXIC",
            "미국 국채 10년물": "^TNX",
            "서부 텍사스유": "CL=F",
            "비트코인": "BTC-USD",
            "금": "GC=F"
        }

    def get_stock_data(self, ticker_symbol):
        try:
            ticker = yf.Ticker(ticker_symbol)
            # Get fast info first (often faster/more reliable for current price)
            info = ticker.fast_info
            
            # fast_info provides 'last_price' and 'previous_close'
            current_price = info.last_price
            prev_close = info.previous_close
            
            if current_price is None:
                # Fallback to history if fast_info fails
                hist = ticker.history(period="1d", interval="1m")
                if not hist.empty:
                    current_price = hist['Close'].iloc[-1]
                    # approximating prev close if needed, or just skipping change,
                    # but let's stick to simple first
                else:
                    return None
            
            change = current_price - prev_close
            change_percent = (change / prev_close) * 100
            
            return {
                "price": current_price,
                "change": change,
                "change_percent": change_percent
            }
        except Exception as e:
            print(f"Error fetching data for {ticker_symbol}: {e}")
            return None

    @app_commands.command(name="주가", description="주요 금융 지표(S&P500, 나스닥, 미국 국채, 유가, 코인, 금)의 실시간 시세를 조회합니다.")
    async def stock_price(self, interaction: discord.Interaction):
        await interaction.response.defer() # Fetching data might take a few seconds

        embed = discord.Embed(
            title="📈 실시간 주요 금융 지표",
            description=f"기준 시간: {datetime.now(pytz.timezone('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')}",
            color=0x00ff00 # Default color
        )

        # Iterate through tickers and add fields
        for name, symbol in self.tickers.items():
            data = self.get_stock_data(symbol)
            
            if data:
                price = data['price']
                change = data['change']
                pct = data['change_percent']
                
                # Formatting
                # Emoji for a direction
                if change > 0:
                    emoji = "🔺"
                    color_code = "+ " # For diff syntax if used, but simple string is better here
                elif change < 0:
                    emoji = "hg" # wait, down arrow
                    emoji = "🔹"
                else:
                    emoji = "➖"

                # Special formatting for different assets (decimlas)
                if "국채" in name:
                    value_str = f"{price:.3f}%"
                elif "비트코인" in name:
                    value_str = f"${price:,.2f}"
                else:
                    value_str = f"{price:,.2f}"
                
                change_str = f"{change:+.2f} ({pct:+.2f}%)"
                
                embed.add_field(
                    name=f"{name} {emoji}",
                    value=f"**{value_str}**\n`{change_str}`",
                    inline=True
                )
            else:
                embed.add_field(
                    name=name,
                    value="데이터 조회 실패",
                    inline=True
                )
        
        embed.set_footer(text="Data provided by Yahoo Finance")
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(FinanceCog(bot))
