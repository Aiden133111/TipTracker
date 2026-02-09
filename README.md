# TipTracker.py
Author: Aiden McFadden
NOT DEVELOPED IN AFFILIATION WITH OPENTRONS
USE AT YOUR OWN RISK

## The Problem
Tracking the tipboxes on the expansion slots and in stackers are not accessibily to pipettes without explicily calling the gripper to move the empty labware away and move the new tiprack from the expansion / stacker over to replace it. Because these are explicit API calls and run time parameters affect tip usage, it makes it hard to track when tip racks need to be replaced.

## The Solution
TipTracker is a class desgined to solve this problem. After setting up the Tracker object, it will use its own version of InstrumentContect.pick_up_tip() commands (as well as many others) to handle when pipettes are out of tips. Instead of having to track all times where you could run out of tips, TipTracker automatically grabs tips from whatever source is available wether it be stackers or expansion slots, without needed explicit API calls or lenthy try blocks. When all tipracks are gone and more are needed, the protocol is paused and prompts the user to add a specified number of tipracks. The method will also dispose of empty tipracks in one of three ways: 1. Through the waste chute. 2. Carousel tips around but keep them on deck until no tips remain and remove manually 3. Manually without carousel. 

## TipTracker Intent
The following sections will descibe how TipTracker is intended to be used, but first I will explain the different cases I have considered in my development
| Component      | Low Throughput | Medium Throughput | High Throughput |
|----------------|----------------|-------------------|-----------------|
| Gripper        |        X       |        Y          |         Y       |
| Waste Chute    |        X       |        X          |         Y       |
| Trash Bin      |        Y       |        Y          |         X       |
| Stacker        |        X       |        X          |         Y       |
| Expansion Slot |        X       |        Y          |         Y       |

Low throughput configurations dont see the benefit from this since they can fit all their tips on the deck and have no method of storing tips off deck. This method is only helpful for them to automattically pause and prompt the user to refill tips if needed. 
Medium throughput configurations have a gripper and can move tips from expansion slots (or possibly a single stacker) to the active deck, but may not have a waste chute, this method serves as a way of shuttling their tips and carouseling empty boxes around until all tips are used since there is no waste chute here
High throughput individuals have a way of disposing of empty tipracks using the waste chute and can have lots of extra tips from multiple stackers (or combinaton of stacker / expasion slots) and can use this method to acheive maximum walk away time. 

## Function Documentation and Examples
This section is dedicated to explaining how the tracker works while nesting in line examples of code using the TipTracker. It is organized as a tutorial in which in the order of function will generally follow the order the code is developed, with more niche functions explained at the end within their own sections

You begin by copy/pasting the method to the top of your protocol. It shouldn't be nested within any other function inclduing the run function.

### Creating the TipTrackerObject
#### Explanation
Create your metadata,requirements, parameter, and run funcions as you normally would. Within the run function, wastebin, pipettes, modules (aside from stackers being used from tracking) and labwares (aside from tipracks) as normal. Below these segments we will create the TipTracker ojbect and customize how we want racks to be replaced within our code.

It can be a good idea to store some aditional constants within your protocol that you might be using regularly with the TipTracker. Storing the API load name for each tip you are using is wise since a lot of the tracker will depend on these load names. Keeping the tiprack slots for each type used can be helpful to edit the slots faster as well as being able to access it later in the code.

Required Parameters: ProtocolContext, A first pipette to use, if you are using a gripper, your waste bin
Optional Parameters: A second pipette, if you want to turn on debugging mode, if you want to supress comments to the run log
#### TipTracker Example
```
import TipTracker #Instead of copy/pasting (will not work on robot, must C/P)
from opentrons import protocol_api
.
.
.
def run(ctx : protocol_api.ProtocolContext)
	single_50 = ctx.load_instrument('flex_1channel_50', 'left',)
	multi_50 = ctx.load_instrument('flex_8channel_1000', 'right')
	chute = ctx.load_waste_chute()
	use_gripper = True

	TipTrackerObject = TipTracker(
		protocol_context=ctx,
		pipette1=single_50, 
		waste_bin=chute,
		use_gripper=use_gripper = True,
		debugging=True,
		pipette2=multi_50,)

	#Common Constants that might be helpful, add for each tip that you are going to use (or comment out others)
	P20_SLOTS = ['C2','D4']
	P50_SLOTS = ['C3']
	P200_SLOTS = ['B3']
	P200_SLOTS = ['B1']
	TIPS20 = 'opentrons_flex_96_tiprack_20ul'
	TIPS50 = 'opentrons_flex_96_tiprack_50ul'
	TIPS200 = 'opentrons_flex_96_tiprack_200ul'
	TIPS1000 = 'opentrons_flex_96_tiprack_1000ul'

```
### Configure Replacement Methods
Determine how your extra tipracks should be added to the deck. This can be adding expansion slots  or Stackers. In Stacker methods we will not directly call load_module() and instead using an internal function. These Stacker and expansion slots should be those you want to be associated with tipracks, you do not need to include all extra hardware if you want to put other labware on it. 

#### Expansion Slots
The following method adds your expansion slots as slots that should be tracked. This means these slots are viable for tips or open slots for shuffling labware. Not all of the slots installed on the robot have to be added via these function. Adding expansion slots via this function prior to assigning tipracks to the slots.

Required Parameters: list of expansion slots that you want to use.
```
	expansion_slots_for_tips = ['A4','B4','C4']
	TipTrackerObject.add_expansion_slots(expansion_slots_for_tips)
```
#### Stackers
If you are using stackers we will add the stacker using an internal method that will return the StackerObject so developers can still interact with it. You may choose to use the returned stacker to load something else onto the shuttle (when load_on_shuttle = False). In these cases you should set TipTracker.open_slot if you want the Tracker to automatically move said labware out of the way to grab new tipracks and then return it. If you want a tiprack on the shuttle set the load_on_shuttle property to True. Lids on tipracks will be thrown away when the labware is dispensed. Lids will be loaded on Tipracks in the stacker when enabled but no on any tipracks on the shuttle or in the deck

If load on shuttle is True and intital_count is less than 7, then the first rack will be on the shuttle and n-1 will be loaded in the stacker.
```
Stacker = TipTrackerObject.add_stacker(slot='D4',
                        rackname=TIPS200,
                        initial_count=7,
                        lid='opentrons_flex_tiprack_lid',
						load_on_carridge=True)
```
or when loading something else on the Shuttle
```
Stacker = TipTrackerObject.add_stacker(slot='D4',
                        rackname=TIPS200,
                        initial_count=4,
                        lid='opentrons_flex_tiprack_lid',
						load_on_carridge=False)
Labware = Stacker.load_labware('nest_96_wellplate_2ml_deep')
TipTrackerObject.open_slot = MagneticBlockContext #This will move the DWP to the magnet whenever we need a tiprack from the stacker
```
### Loading Tipracks to the Deck
Instead of loading our tipracks with the load labware function, we will use add_starting_tipracks which takes a pair consisting of a API tiprack load name and the slots you want to assign to it. It will load a tiprack in that slot, and add all slots to the tiprack assignment, Meaning should we run out of said tip, it should be refilled in all of these slots. We can add up to four tiprack-slot pairs to the tracker at a time.
```
	TipTrackerObject.add_starting_tipracks(
		tiprack1 = 'opentrons_flex_96_filtertiprack_200ul',
		slots1 = ['A1','B1','B4'],
		tiprack2 = 'opentrons_flex_96_filtertiprack_50ul',
		slots2 = ['A2','A3','B2'],
		tiprack3 = 'opentrons_flex_96_filtertiprack_1000ul',
		slots3 = ['B3'])
```
### Configuration Special Considerations
There are a few routine operations where additional steps to configure the TipTracker might be necessary to either make the protocol easier to code or to even make it possible. This section is for some of these situations

#### Carouseling Tips (No Dumpting Racks Down Waste Chute)
Not having a waste chute for higher throughput applications is severly limiting, but it does not make the TipTracker unusable. Since we can no longer dump empty tipracks off of the deck, we must now find other slots to hold them in while we shuffle stuff around the deck. We do this by setting an open_slot property
```
	TipTrackerObject.use_chute = False
	TipTrackerObject.carousel_tips = True
	TipTrackerObject.open_slot = 'A3'
```
In these three lines, I first tell the Tracker that I do not want to use the Waste chute to dispose of tipracks. This is automatiically set to False when not using the waste chute. I then tell the tracker that I want my empty racks to be carouselled around the deck instead. This is automcally True when not using the waste chute. I then say I will reserve the A3 slot for my first empty rack. It will update this property as necessary as labware is moved around the deck. 

#### Partial Tip Pickup
Although it is a powerful tool, partial tip pickup is annoying to code because of the spacial limitations of tall labware and the gantry. Often while coding you have to make a deck setup where your partial pickup is not going to be broken by other routine motions. We can simpify our approach by tailoring the TipTracker to force all pickup options for a given pipette to occur at a given slot. This means I only have to worry about collisions tiprack slot and when the pipette is out of tips it will move the empty tiprack out of the way and refill it with another tiprack on the deck. 
```
	ctx.load_trash_bin('D3')
	TipTrackerObject.open_slot = 'A4'
	TipTrackerObject.pick_up_slots['opentrons_flex_96_filtertiprack_50ul'] = 'A1'
```
This will force all pickup actions for the 50µL Tip to happen on A1. In this example, because I am using a trash bin, I must also set a open_slot. This means that when the tips in A1 are empty, they will be moved to A4 and then my next tiprack can fill the now empty A1. Pickup slots should be defiend after starting tipracks are added but before any pick up actions that should be on this slot.

#### Reusing Tips and Ignoring Slots
There are many times that you may want to return tips to a tiprack to use later in the protocol. Because these tips are marked as used it will cause the protocol context to believe the rack is empty. To prevent reusable racks from being replaced, we set the ignore_slots property. Imagine a siutation where we have loaded a tiprack into slot A1,
```
	TipTrackerObject.ignore_slots.append('A1')
```
will prevent any dumping or replacing of the rack in A1 if it is used or not. This will not avoid picking up tips from this slot, just prevent getting rid of the tiprack when refills are called. 

### Putting it All Together
We have created and set up our tracker, now comes the fun part. Actually using it! This section details the three functions that are most used, tip assignment, pickups, drops.

#### Assigning Pipettes and Tipracks
A lot of the work with the tracker comes down to 2 general parameters: the pipette you want to use and the tiprack you want to use. To keep from specifying this combination each time we call a function (although valid) we can assign an acitve pipette.
```
	single_50 = ctx.load_instrument('flex_1channel_50', 'left',)
	TipTrackerObject.active_pipette = single_50
```
Now I can call assign_tipracks(), pick_up(), and drop_tip() commands without passing a pipette and it will use the `single_50` pipette until otherwise specified or the active_pipette is changed


When we are wanting to attach tipracks to a pipette we call the assign_tipracks function instead of calling InstrumentContext.tip_racks = list[]. You only must provide a rack name if you have an active pipette set, otherwise rack_name and the pipette you want to use are required. This does not pickup a tip, just prepares the pipette for what tips it should be using.
```
	TipTrackerObject.assign_tipracks(
		rack_name = 'opentrons_flex_96_filtertiprack_50ul'
		)
```
#### Pickups and Drops
The two most common functions are going to be pickup and drop. 75% of this entire package is run through the pick_up function. Pick_up will return a interger value linked to how it was able to pickup a tip wether there was already one available, it had to move something from a stacker, or it required manual intervention. Pickup has no required arugments with an active_pipette preset. Otherwise only pipette is required. It can optionally take a Location tip to pickup in the case you want a specific tip and a boolean to refill all tips when any tip is out (to minimize manual intervention).

The drop_tip function is largely a 1:1 replacement of InstrumentContect.drop_tip(). It takes an optional InsturmentContect parameter if no active_pipette is set, a location argument if you are wanting to drop the tip in any given well and may take a return_tip boolean if you are wanting to return the tip instead of trashing it.
*Pro Tip* Use `TipTrackerObject.drop_tip(return_tip=DryRunParameter)` to alwasy return your tips during dry runs instead of havings lots of `if DryRun: return_tip` blocks.
```
	for i in range (47):
		TipTrackerObject.pick_up(
			pipette = single_50)
		TipTrackerObject.drop_tip(
			pipette = single_50,
			return_tip = True)
```
### Setting Max rack limits 
By default the tracker will refill all tip slots for a given racktype when it runs out, but this becomes problematic if we only need one or two more tipracks close to the end of the run. As developers we must understand how many tips a protocol is going to use since this protocol uses the load-as-you-go method. We determine the amount of tips we use during a particular protocol using the 

```
TipTrackerObject.tip_count = {rackName : int for rackname in self.tipracks} # Amount of tips pickedup
TipTrackerObject.tip_rack_count = {rackName : int for rackname in self.tipracks} #Amount of tipracks loaded 
```
by printing the tip_count property after a protocol using `ProtocolContext.comment(f'{TipTrackerObject.tip_count}')` and celing divding all counts by 12 you can find the amount of tips used in a given simulation. Note the tip_rack_count property has no ceiling at this point so it may not be the same as the calculated integer 

We can set the max counts by doing the following
```
TipTrackerObject.max_rack_count[rackName] = int
```

### Troubleshooting
When setting up our protocol we may want to track what the tracker is doing when protocols are failing or we may or may not want the protocol to print comments to the user about its actions. We can do the following with a couple of arguments when defining the TipTrackerObject

```
TipTrackerObject = TipTracker(
		protocol_context=ctx,
		pipette1=single_50, 
		pipette2=multi_50,
		waste_bin=chute,
		use_gripper=use_gripper = True,
		debugging=True,
		suppress_comments=True
)
```

Setting debugging to True will print its actions as print commands and is useful for checking to make sure the right pipette is being used at a given time or the deck is resetting when you expect it (uses `print()` commands). Setting suppress_comments to True will remove the those same comments from being displayed to the user during RunTime.

You can also `print(TipTrackerObject.pick_up_tip())` to see what was needed for a given tip pick up. Right now this returns an integer corresponding to the motions needed to pick up the tip.


Thats the basics! Keep assigning tips as necessary and the protocol will automatically move tipracks around as needed and also pause if it doesn't have enough. 
