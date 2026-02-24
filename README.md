# TipTracker.py
Author: Aiden McFadden
NOT DEVELOPED IN AFFILIATION WITH OPENTRONS
USE AT YOUR OWN RISK

## The Problem
Tracking the tip boxes on the expansion slots and in stackers are not accessibly to pipettes without explicitly calling the gripper to move the empty labware away and move the new tiprack from the expansion / stacker over to replace it. Because these are explicit API calls and run time parameters affect tip usage, it makes it hard to track when tip racks need to be replaced.

## The Solution
TipTracker is a class designed to solve this problem. After setting up the Tracker object, it will use its own version of `InstrumentContect.pick_up_tip()` commands (as well as many others) to handle when pipettes are out of tips. Instead of having to track all times where you could run out of tips, TipTracker automatically grabs tips from whatever source is available wether it be stackers or expansion slots, without needed explicit API calls or lengthy try blocks. When all tip racks are gone and more are needed, the protocol is paused and prompts the user to add a specified number of tip racks. The method will also dispose of empty tip racks in one of three ways: 1. Through the waste chute. 2. Carousel tips around but keep them on deck until no tips remain and remove manually 3. Manually without carousel. 

## TipTracker Intent
The following sections will describe how TipTracker is intended to be used, but first I will explain the different cases I have considered in my development
| Component      | Low Throughput | Medium Throughput | High Throughput |
|----------------|----------------|-------------------|-----------------|
| Gripper        |        X       |        Y          |         Y       |
| Waste Chute    |        X       |        X          |         Y       |
| Trash Bin      |        Y       |        Y          |         X       |
| Stacker        |        X       |        X          |         Y       |
| Expansion Slot |        X       |        Y          |         Y       |

Low throughput configurations don't see the benefit from this since they can fit all their tips on the deck and have no method of storing tips off deck. This method is only helpful for them to automatically pause and prompt the user to refill tips if needed. 
Medium throughput configurations have a gripper and can move tips from expansion slots (or possibly a single stacker) to the active deck, but may not have a waste chute, this method serves as a way of shuttling their tips and carouseling empty boxes around until all tips are used since there is no waste chute here
High throughput individuals have a way of disposing of empty tip racks using the waste chute and can have lots of extra tips from multiple stackers (or combination of stacker / expansion slots) and can use this method to achieve maximum walk away time. 

## Function Documentation and Examples
This section is dedicated to explaining how the tracker works while nesting in line examples of code using the TipTracker. It is organized as a tutorial in which in the order of function will generally follow the order the code is developed, with more niche functions explained at the end within their own sections

You begin by copy/pasting the method to the top of your protocol. It shouldn't be nested within any other function including the run function.

### Creating the TipTrackerObject
#### Explanation
Create your metadata, requirements, parameter, and run functions as you normally would. Within the run function, waste-bin, pipettes, modules (aside from stackers being used from tracking) and labware (aside from tip racks) as normal. Below these segments we will create the TipTracker object and customize how we want racks to be replaced within our code.

It can be a good idea to store some additional constants within your protocol that you might be using regularly with the TipTracker. Storing the API load name for each tip you are using is wise since a lot of the tracker will depend on these load names. Keeping the tiprack slots for each type used can be helpful to edit the slots faster as well as being able to access it later in the code.

Required Parameters: ProtocolContext, A first pipette to use, if you are using a gripper, your waste bin
Optional Parameters: A second pipette, if you want to turn on debugging mode, if you want to suppress comments to the run log
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
Determine how your extra tip racks should be added to the deck. This can be adding expansion slots  or Stackers. In Stacker methods we will not directly call load_module() and instead using an internal function. These Stacker and expansion slots should be those you want to be associated with tip racks, you do not need to include all extra hardware if you want to put other labware on it. 

#### Expansion Slots
The following method adds your expansion slots as slots that should be tracked. This means these slots are viable for tips or open slots for shuffling labware. Not all of the slots installed on the robot have to be added via these function. Adding expansion slots via this function prior to assigning tip racks to the slots.

Required Parameters: list of expansion slots that you want to use.
```
	expansion_slots_for_tips = ['A4','B4','C4']
	TipTrackerObject.add_expansion_slots(expansion_slots_for_tips)
```
#### Stackers
If you are using stackers we will add the stacker using an internal method that will return the StackerObject so developers can still interact with it. You may choose to use the returned stacker to load something else onto the shuttle (when `load_on_shuttle = False`). In these cases you should set `TipTrackerObject.open_slot` if you want the Tracker to automatically move said labware out of the way to grab new tip racks and then return it. If you want a tiprack on the shuttle set the `load_on_shuttle` property to `True`. Lids on tip racks will be thrown away when the labware is dispensed. Lids will be loaded on Tip racks in the stacker when enabled but no on any tip racks on the shuttle or in the deck

If load on shuttle is `True` and `intital_count` is less than 7, then the first rack will be on the shuttle and n-1 will be loaded in the stacker.
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
### Loading Tip racks to the Deck
Instead of loading our tip racks with the load labware function, we will use `add_starting_tipracks` which takes a pair consisting of a API tiprack load name and the slots you want to assign to it. It will load a tiprack in that slot, and add all slots to the tiprack assignment. Meaning should we run out of said tip, it should be refilled in all of these slots. We can add up to four tiprack-slot pairs to the tracker at a time.
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
There are a few routine operations where additional steps to configure the `TipTracker` might be necessary to make the protocol easier to code or to even make it possible. This section is for some of these situations.

#### Carouseling Tips (No Dumping Racks Down Waste Chute)
Not having a waste chute for higher throughput applications is severely limiting, but it does not make the `TipTracker` unusable. Since we can no longer dump empty tip racks off of the deck, we must now find other slots to hold them in while we shuffle stuff around the deck. We do this by setting an open_slot property:
```
	TipTrackerObject.use_chute = False
	TipTrackerObject.carousel_tips = True
	TipTrackerObject.open_slot = 'A3'
```
In the first line, the `use_chute` property is turned to `False` which prevents tip racks from being thrown down the chute when installed. This is automatically `False` when not using the waste chute, and does not have to be directly called. In the next line, the TipTrackerObject is configured to carousel empty racks around the deck. This is default `True` when not using the waste chute. In the next line, the A3 slot is desisgnated as slot to recieve an empty tiprack when we need to move things around the deck. It will update this property as necessary as labware is moved around the deck. 

Note that stackers can also be used to store empty tipracks to prevent them from being throw out

#### Partial Tip Pickup
Although it is a powerful tool, partial tip pickup can be difficult to code because of the spacial limitations of tall labware and the gantry bounds. A lot of time id dedicated to coding the deck setup so partial pickup is not going to be broken by other routine motions. We can simplify our approach by tailoring the TipTrackerObject to force all pickup options for a given pipette to occur at a given slot. This means a mironity of slots have to be considered for collisions and pipette bounds regarding pick up. When the pipette is out of tips it will move the empty tiprack out of the way and refill it with another tiprack on the deck into the same slot. 
```
	ctx.load_trash_bin('D3')
	TipTrackerObject.open_slot = 'A4'
	TipTrackerObject.pick_up_slots['opentrons_flex_96_filtertiprack_50ul'] = 'A1'
```
An `open_slot` is designated in this example since a trash bin is being used. In the final line, a slot is added as the dictionary entry to a tiprack. This will force all pickup actions for the 50µL Tip to happen on A1. When the tips in A1 are empty, they will be moved to A4 and then my next tiprack can fill the now empty A1. Pickup slots should be defined after starting tip racks are added but before any pick up actions that should be on this slot.

#### Reusing Tips and Ignoring Slots
There are many times that you may want to return tips to a tiprack to use later in the protocol. Because these tips are marked as used it will cause the protocol context to believe the rack is empty. To prevent reusable racks from being replaced, we set the ignore_slots property. Imagine a situation where we have loaded a tiprack into slot A1,
```
	TipTrackerObject.ignore_slots.append('A1')
```
will prevent any dumping or replacing of the rack in A1 if it is used or not. This will not avoid picking up tips from this slot, just prevent getting rid of the tiprack when refills are called. 


#### 96 Channel Pipetting
New in TipTracker v2.2

Adapters for 96 channel pipetting can now be used within the tracker. The tracker will replace tip boxes on adapters and throw away the empty ones. Adapters may be specific to a certain tip rack type or can be available to all tip racks and a tiprack will be moved an adapter according to the type of tip currently assigned to the pipette. The tracker can also reconfigure the pipette depending on the pickup you are using by passing the assign tip racks function the mode that you want to configure and the start / stop nozzle when necessary. Extra tip racks of a given type (ie those without a tip missing) are all considered valid replacements for an adapter pickup unless that slot in ignored_slots even if it is the only other tiprack on the deck of that type (although will prioritize refilling from expansions slots and stackers).

We add adapters when setting up the tip tracker with initial tip racks with the `adapters` argument. By default adapters added will be assigned to the tiprack with the overlapping slot
```
	TipTrackerObject.add_starting_tipracks('opentrons_flex_96_filtertiprack_50ul', ['C1'], 'opentrons_flex_96_filtertiprack_1000ul', ['A1','A3'],adapters=['A1','C1'])
```
We can change the tiprack that should be replace onto an adapter by just assigning the slot to another rack type:
```
	TipTrackerObject.assign_slots('opentrons_flex_96_filtertiprack_50ul',['A1'])
```
since we have already loaded an adapter in the slot we only need to tell it the slot and the tiprack that should be used and we do not need to mention the adapters directly.

We can then distinguish between the P1000 tip racks based on how we call `assign_tipracks`. The below will automatically configure the current active pipette's nozzle layout and assign it the P1000 tip racks specifically on the adapters.
```
	TipTrackerObject.assign_tipracks('opentrons_flex_96_filtertiprack_1000ul',mode = protocol_api.ALL)
```
The above would assign the pipette to the tiprack/adapter system on A1. If you want a 96 channel pipette to pick up a column of tips using the A12 nozzle of the same type you could do 
```
	TipTrackerObject.assign_tipracks('opentrons_flex_96_filtertiprack_1000ul',mode = protocol_api.COLUMN, start='A12')
```
to change the pipette to pick up the tips in a column fashion on A3. As long as the nozzle configuration is not 'ALL' it will assign tips not on an adapter, so this method can work for any partial tip configuration, but it will not discriminate further between tip racks used for certain nozzle configurations. If you want them to be separate, consider loading one type as a filter tip and the other as a regular tip.  The same modes can be used for 8-channel pipettes in partial configurations. A `start` and `end` nozzle parameter are needed for `PARTIAL_COLUMN` configurations. Use `COLUMN` for 8-channel full column pickups instead of `ALL`

In some situations, you might want to limit the adapters to save deck space but still want to pick up multiple different tiprack types from adapters. In these situations, we can set our adapters to be globally accessible by any tiprack type and the tracker will move the appropriate tiprack to the adapter when a `pick_up` is called.
```
	TipTrackerObject.add_starting_tipracks(opentrons_flex_96_filtertiprack_50ul, ['B1'] , opentrons_flex_96_filtertiprack_200ul, ['C1'], opentrons_flex_96_filtertiprack_1000ul, ['A1','A3'], adapters=['A1'])
	TipTrackerObject.global_adapter = True

	#Pick up and drop P1000 tips on A1, then A3 (after moving to adapter)
	TipTrackerObject.assign_tipracks('opentrons_flex_96_filtertiprack_1000ul', mode=protocol_api.ALL,)
	for i in range(2):
		TipTrackerObject.pick_up()
		TipTrackerObject.drop()

	#Assign P200s ALL format even though no adapter is directly assigned
	TipTrackerObject.assign_tipracks('opentrons_flex_96_filtertiprack_200ul', mode=protocol_api.ALL)
	TipTrackerObject.pick_up()
	TipTrackerObject.drop()
```
When `pick_up` is called after assigning the pipette to the P200 tip racks, the tracker will realize no adapters are assigned to P200 tips. It will dispose of the used P1000 tiprack on the adapter and replace it with the P200 tiprack on slot C1 since global adapters is `True`. If another P200 `pick_up` is called, they will be refilled on the adapter and slot C1. P1000 tip racks would only be refilled on A3 since the adapter is currently assigned to the P200 tiprack.


### Putting it All Together
We have created and set up our tracker, now comes the fun part. Actually using it! This section details the three functions that are most used, tip assignment, pickups, drops.

#### Assigning Pipettes and Tip racks
A lot of the work with the tracker comes down to 2 general parameters: the pipette you want to use and the tiprack you want to use. To keep from specifying this combination each time we call a function (although valid) we can assign an active pipette.
```
	TipTrackerObject.active_pipette = single_50
```
Now I can call assign_tipracks(), pick_up(), and drop_tip() commands without passing a pipette and it will use the `single_50` pipette unless another pipette is specified or the active_pipette is changed.


When we are wanting to attach tip racks to a pipette we call the `assign_tipracks` function instead of calling InstrumentContext.tip_racks =[]. You only must provide a rack name if you have an active pipette set, otherwise rack_name and the pipette you want to use are required. This does not pickup a tip, just prepares the pipette for what tips it should be using.
```
	TipTrackerObject.assign_tipracks(
		rack_name = 'opentrons_flex_96_filtertiprack_50ul'
		)
```
#### Pickups and Drops
The two most common functions are going to be pickup and drop. 75% of this entire package is run through the pick_up function. Pick_up will return a integer value linked to how it was able to pickup a tip wether there was already one available, it had to move something from a stacker, or it required manual intervention. Pickup has no required arguments with an active_pipette preset. Otherwise only pipette is required. It can optionally take a Location tip to pickup in the case you want a specific tip and a boolean to refill all tips when any tip is out (to minimize manual intervention).

The drop_tip function is largely a 1:1 replacement of `InstrumentContect.drop_tip()`. It takes an optional `InsturmentContect` parameter if no active_pipette is set, a location argument if you are wanting to drop the tip in any given well and may take a return_tip boolean if you are wanting to return the tip instead of trashing it.
*Pro Tip* Use `TipTrackerObject.drop_tip(return_tip=DryRunParameter)` to always return your tips during dry runs instead of having lots of `if DryRun: return_tip` blocks. Unless you are using partial tip pickup
```
	for i in range (47):
		TipTrackerObject.pick_up(
			pipette = single_50)
		TipTrackerObject.drop_tip(
			pipette = single_50,
			return_tip = True)
```
### Setting Max rack limits 
By default the tracker will refill all tip slots for a given tip rack type when it runs out, but this becomes problematic if we only need one or two more tip racks close to the end of the run. As developers we must understand how many tips a protocol is going to use since this protocol uses the load-as-you-go method. We determine the amount of tips we use during a particular protocol using the 

```
TipTrackerObject.tip_count = {rackName : int for rackname in self.tipracks} # Amount of tips pickedup
TipTrackerObject.tip_rack_count = {rackName : int for rackname in self.tipracks} #Amount of tipracks loaded 
```
by printing the tip_count property after a protocol using `ProtocolContext.comment(f'{TipTrackerObject.tip_count}')` and ceiling dividing all counts by 12 you can find the amount of tips used in a given simulation. Note the `tip_rack_count `property has no ceiling at this point so it may not be the same as the calculated integer 

We can set the max counts by doing the following
```
TipTrackerObject.max_rack_count[rackName] = int
```

### Troubleshooting
When setting up our protocol we may want to track what the tracker is doing when protocols are failing or we may or may not want the protocol to print comments to the user about its actions. We can do the following with a couple of arguments when defining the `TipTrackerObject`

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

Setting debugging to True will print its actions as print commands and is useful for checking to make sure the right pipette is being used at a given time or the deck is resetting when you expect it (uses `print()` commands). Setting `suppress_comments` to `True` will remove the those same comments from being displayed to the user during RunTime.

You can also `print(TipTrackerObject.pick_up_tip())` to see what was needed for a given tip pick up. Right now this returns an integer corresponding to the motions needed to pick up the tip.


Thats the basics! Keep assigning tips as necessary and the protocol will automatically move tip racks around as needed and also pause if it doesn't have enough.
