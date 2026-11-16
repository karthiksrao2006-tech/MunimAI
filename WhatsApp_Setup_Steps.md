# WhatsApp Cloud API — test number setup (free, ~15-20 min)

No business documents, no payment method, no dedicated number needed. The free
test number can message up to 5 phone numbers you verify yourself.

## Before you start
- A Facebook account (personal is fine)
- Your phone with WhatsApp, to receive test messages

## Part A — Developer account
1. Go to developers.facebook.com
2. Log in with Facebook (top right)
3. First time: Get Started -> accept terms -> role = Developer

## Part B — Create the app
4. developers.facebook.com/apps -> Create app
5. Use case: select **Other** -> Next   (this reveals the Business type)
6. App type: **Business** -> Next
7. App name: MunimAI ; contact email: yours ; business portfolio: none for now
8. Create app -> enter Facebook password if asked

## Part C — Add WhatsApp, get the test number
9. App dashboard -> Add products -> find **WhatsApp** -> Set up
10. Create or select a Meta Business Account -> Create a business account -> Continue
11. You land on **WhatsApp -> API Setup**. The test "From" number is already
    created; note its **Phone number ID** below it.

## Part D — Add your phone as a recipient
12. On API Setup, the "To" field -> Manage phone number list -> Add phone number
13. Enter your number with country code (+91...) -> verify with the code
14. Repeat for up to 4 more (5 total maximum)

## Part E — Send a test message
15. Select your number in "To" -> click **Send message**
16. Check your phone for a WhatsApp from the test number. If it arrives, done.

## Part F — The three values, into .env (NOT into chat)
Add these lines to  D:\param shavak\project KS\.env :

    WHATSAPP_PHONE_NUMBER_ID=<the Phone number ID, not secret>
    WHATSAPP_BUSINESS_ACCOUNT_ID=<the WhatsApp Business Account ID, not secret>
    WHATSAPP_TOKEN=<the temporary access token, SECRET - EAAG...>

Then tell Claude "done".

## Notes
- The access token is TEMPORARY (expires in 24h). Fine for building/testing.
  A permanent System User token is a 5-minute step for later, not now.
- The test number can only message the 5 numbers you verified. Outside judges
  cannot message it directly without full production setup (business
  verification, several days). The dashboard + demo recording cover this.
